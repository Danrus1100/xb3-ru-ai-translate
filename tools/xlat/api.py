"""Клиент OpenAI-совместимого и Anthropic-совместимого API (stdlib-only).

Читает `config/api.json`:
    base_url        полный URL эндпоинта (клиент ничего не дописывает)
    api_key         ключ; если пусто — берётся из переменной окружения api_key_env
    model           имя модели
    api_style       "openai" (chat/completions) | "anthropic" (/messages)
    temperature, top_p, max_tokens, timeout_s, max_retries, min_interval_ms
    extra_headers   дополнительные заголовки (например x-opencode-session)
    extra_body      дополнительные поля тела запроса

Учитывает usage (prompt/completion tokens) и суммарную стоимость прогона.
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


def load_config(path: str = "config/api.json") -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    if not cfg.get("api_key"):
        cfg["api_key"] = os.environ.get(cfg.get("api_key_env", ""), "")
    if not cfg.get("api_key"):
        raise ApiError(f"в {path} нет api_key и переменная {cfg.get('api_key_env')} пуста")
    if not cfg.get("model"):
        raise ApiError(f"в {path} не задана model")
    return cfg


class ChatClient:
    """Один запрос = один вызов `complete(system, user)`."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self._last_call = 0.0

    # --- низкий уровень -------------------------------------------------
    def _body(self, system: str, user: str) -> dict:
        style = self.cfg.get("api_style", "openai")
        body = {
            "model": self.cfg["model"],
            "max_tokens": self.cfg.get("max_tokens", 4096),
            "temperature": self.cfg.get("temperature", 0.2),
            "top_p": self.cfg.get("top_p", 1.0),
        }
        if style == "anthropic":
            body["system"] = system
            body["messages"] = [{"role": "user", "content": user}]
        else:
            body["messages"] = [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
            if self.cfg.get("json_mode"):
                body["response_format"] = {"type": "json_object"}
        body.update(self.cfg.get("extra_body") or {})
        return body

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        h["Authorization"] = f"Bearer {self.cfg['api_key']}"
        h.update(self.cfg.get("extra_headers") or {})
        return h

    @staticmethod
    def _extract(data: dict) -> str:
        if "choices" in data:
            msg = data["choices"][0].get("message", {})
            content = msg.get("content")
            if isinstance(content, list):  # некоторые шлюзы отдают блоки
                return "".join(part.get("text", "") for part in content if isinstance(part, dict))
            return content or ""
        if "content" in data:  # Anthropic
            parts = data["content"]
            if isinstance(parts, list):
                return "".join(p.get("text", "") for p in parts if isinstance(p, dict))
            return str(parts)
        raise ApiError("в ответе нет ни choices, ни content", body=json.dumps(data)[:300])

    def complete(self, system: str, user: str) -> str:
        interval = self.cfg.get("min_interval_ms", 0) / 1000.0
        wait = self._last_call + interval - time.time()
        if wait > 0:
            time.sleep(wait)
        body = json.dumps(self._body(system, user), ensure_ascii=False).encode("utf-8")
        attempts = int(self.cfg.get("max_retries", 5))
        last_err: Exception | None = None
        for attempt in range(attempts + 1):
            req = urllib.request.Request(
                self.cfg["base_url"], data=body, headers=self._headers(), method="POST"
            )
            try:
                with urllib.request.urlopen(req, timeout=self.cfg.get("timeout_s", 180)) as resp:
                    data = json.load(resp)
                self.calls += 1
                self._last_call = time.time()
                usage = data.get("usage") or {}
                self.tokens_in += int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
                self.tokens_out += int(
                    usage.get("completion_tokens") or usage.get("output_tokens") or 0
                )
                return self._extract(data)
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", "replace")
                last_err = ApiError(f"HTTP {e.code}: {text[:300]}", e.code, text)
                if e.code not in (408, 409, 425, 429, 500, 502, 503, 504):
                    raise last_err
            except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
                last_err = e
            time.sleep(min(60.0, (2 ** attempt) + random.random()))
        raise ApiError(f"запрос не удался за {attempts + 1} попыток: {last_err}")

    def summary(self) -> str:
        return (
            f"запросов: {self.calls}, токенов: {self.tokens_in} → {self.tokens_out}"
            f" (модель {self.cfg['model']})"
        )


def parse_json_answer(text: str) -> dict:
    """Достаёт JSON-объект из ответа модели (снимает ```-обёртку)."""
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t[:4].lower() == "json":
            t = t[4:]
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ApiError(f"в ответе нет JSON: {text[:200]!r}")
    return json.loads(t[start : end + 1])


def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="xlat.api")
    ap.add_argument("--config", default="config/api.json")
    ap.add_argument("--ping", action="store_true", help="один минимальный запрос")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    print(f"эндпоинт: {cfg['base_url']}\nмодель: {cfg['model']} (style={cfg.get('api_style','openai')})")
    client = ChatClient(cfg)
    if args.ping:
        answer = client.complete(
            "Отвечай одним словом.", "Ответь: pong"
        )
        print(f"ответ: {answer!r}\n{client.summary()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
