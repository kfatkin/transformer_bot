import asyncio
import json
import logging
import re
from typing import Any

import httpx

from app.core.config import Settings
from app.core.models import Evaluation
from app.core.store import Store

logger = logging.getLogger(__name__)


class Advisor:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.store = store
        self.queue: asyncio.Queue[Evaluation] = asyncio.Queue(maxsize=50)

    async def submit(self, evaluation: Evaluation) -> None:
        if not self.settings.advisor_enabled:
            return
        try:
            self.queue.put_nowait(evaluation)
        except asyncio.QueueFull:
            await self.store.log(
                "error",
                {
                    "component": "advisor",
                    "message": "Advisor queue full; rationale skipped",
                    "trade_id": evaluation.trade_id,
                },
            )

    async def run(self) -> None:
        async with httpx.AsyncClient(timeout=12) as client:
            while True:
                evaluation = await self.queue.get()
                try:
                    text = await self.reason(client, evaluation)
                    await self.store.log(
                        "advisor",
                        {
                            "trade_id": evaluation.trade_id,
                            "timestamp": evaluation.timestamp.isoformat(),
                            "kind": evaluation.kind,
                            "model": self.settings.advisor_model,
                            "text": text,
                        },
                    )
                except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
                    logger.exception("Advisor request failed")
                    await self.store.log(
                        "error",
                        {
                            "component": "advisor",
                            "message": str(exc),
                            "trade_id": evaluation.trade_id,
                        },
                    )
                finally:
                    self.queue.task_done()

    async def reason(self, client: httpx.AsyncClient, evaluation: Evaluation) -> str:
        context = {
            "strategy": evaluation.strategy,
            "event": evaluation.kind,
            "transform_credit": str(evaluation.transformer_credit),
            "target": str(evaluation.target_credit),
            "vix": str(evaluation.vix),
            **evaluation.context,
        }
        focus = (
            "Discuss the contrarian entry signal, quote quality, and whether adjustment credit "
            "covers the original debit and all modeled opening/adjustment commissions. "
            "Do not discuss front/back IV: all butterfly contracts have the same expiration. "
            if evaluation.strategy == "ZERO_DTE_BUTTERFLY"
            else "Discuss front/back IV ratio (above 1 means backwardation), quote quality, "
            "and whether the arithmetic transform threshold was met. "
        )
        prompt = (
            "Explain this SPX paper-trade observation in exactly two short sentences. "
            + focus
            + "Unknown values are unavailable, never infer them. No investment advice, "
            "no claim of guaranteed execution. Return only JSON with key 'sentences', "
            "an array of exactly two strings, each a single complete sentence ending "
            f"in a period, without abbreviations or Markdown. Facts: {context}"
        )
        settings = self.settings
        url = settings.advisor_base_url.rstrip("/")
        payload: dict[str, Any]
        if settings.advisor_provider == "ollama":
            response = await client.post(
                f"{url}/api/generate",
                json={
                    "model": settings.advisor_model,
                    "prompt": prompt,
                    "stream": False,
                },
            )
            response.raise_for_status()
            text = response.json()["response"]
        elif settings.advisor_provider == "anthropic":
            response = await client.post(
                f"{url}/v1/messages",
                headers={
                    "x-api-key": settings.advisor_api_key,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": settings.advisor_model,
                    "max_tokens": 180,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            response.raise_for_status()
            text = response.json()["content"][0]["text"]
        else:
            payload = {
                "model": settings.advisor_model,
                "max_tokens": 180,
                "messages": [{"role": "user", "content": prompt}],
            }
            response = await client.post(
                f"{url}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.advisor_api_key}",
                },
                json=payload,
            )
            response.raise_for_status()
            text = response.json()["choices"][0]["message"]["content"]
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Advisor returned an empty or non-text response")
        parsed = json.loads(text)
        sentences = parsed.get("sentences") if isinstance(parsed, dict) else None
        if (
            not isinstance(sentences, list)
            or len(sentences) != 2
            or any(
                not isinstance(sentence, str)
                or not sentence.strip().endswith(".")
                or len(re.findall(r"[.!?](?:\s|$)", sentence.strip())) != 1
                for sentence in sentences
            )
        ):
            raise ValueError("Advisor response must contain exactly two complete sentences")
        return " ".join(sentence.strip() for sentence in sentences)
