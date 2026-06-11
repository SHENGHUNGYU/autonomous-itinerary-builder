"""
LLM 服務層 - 接自架 vLLM（OpenAI 相容 API）

提供：
- get_chat_llm(): 取得設定好的 ChatOpenAI（支援 tool-calling，給 ReAct agents 用）
- call_structured(): 用 json_schema 結構化輸出（對應 vLLM guided_json），含 robust fallback
- extract_json_from_text / safe_parse_pydantic: 解析容錯工具

環境變數：
- OPENAI_BASE_URL  例 http://<vllm-host>:8000/v1
- OPENAI_API_KEY   vLLM 可為任意 dummy 值（如 "EMPTY"）
- OPENAI_MODEL     vLLM 載入的模型名稱
"""

from __future__ import annotations

import json
import os
import re
from typing import Type, TypeVar

from dotenv import load_dotenv
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

load_dotenv()

T = TypeVar("T", bound=BaseModel)


def _normalize_base_url(url: str) -> str:
    """確保 base_url 指向 OpenAI 相容根路徑（vLLM 為 /v1），避免漏 /v1 造成 404。"""
    url = url.rstrip("/")
    if not url.endswith("/v1"):
        url = url + "/v1"
    return url


# 各家 OpenAI 相容端點（沿用 ChatOpenAI，免換 SDK）。
_GEMINI_DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
_NIM_DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
_GROQ_DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"


def _resolve_config() -> tuple[str, str, str, str]:
    """回傳 (backend, base_url, api_key, model)。

    LLM_BACKEND ∈ {nim, gemini, groq, mlx, ollama, vllm}（預設 vllm）。皆走 OpenAI 相容端點：
      - nim    : NVIDIA NIM（integrate.api.nvidia.com/v1），金鑰讀 NVIDIA_API_KEY / NIM_API_KEY
      - gemini : base_url 為 .../openai/（不補 /v1），金鑰讀 GEMINI_API_KEY / GOOGLE_API_KEY
      - groq   : Groq（api.groq.com/openai/v1），金鑰讀 GROQ_API_KEY / OPENAI_API_KEY
      - mlx    : 本機 mlx_lm.server（OpenAI 相容，預設 127.0.0.1:8080/v1），不送 vLLM 專屬欄位
      - vllm/ollama : base_url 補 /v1
    """
    backend = os.getenv("LLM_BACKEND", "vllm").strip().lower()

    if backend == "mlx":
        # 本機 mlx_lm.server（Apple Silicon）：OpenAI 相容端點，套用模型自帶 chat template。
        # 走 ChatOpenAI，但 extra_body 維持空（不送 chat_template_kwargs，避免 400）。
        raw = (os.getenv("MLX_BASE_URL") or os.getenv("OPENAI_BASE_URL", "http://127.0.0.1:8080/v1")).rstrip("/")
        base_url = _normalize_base_url(raw)
        api_key = os.getenv("OPENAI_API_KEY", "mlx")
        model = os.getenv("MLX_MODEL") or os.getenv("OPENAI_MODEL", "mlx-community/Qwen2.5-7B-Instruct-4bit")
        return backend, base_url, api_key, model

    if backend == "groq":
        base_url = os.getenv("GROQ_BASE_URL", _GROQ_DEFAULT_BASE_URL).rstrip("/")
        api_key = os.getenv("GROQ_API_KEY") or os.getenv("OPENAI_API_KEY", "")
        model = os.getenv("GROQ_MODEL") or os.getenv("OPENAI_MODEL", "openai/gpt-oss-120b")
        return backend, base_url, api_key, model

    if backend == "nim":
        base_url = os.getenv("NIM_BASE_URL", _NIM_DEFAULT_BASE_URL).rstrip("/")
        api_key = os.getenv("NVIDIA_API_KEY") or os.getenv("NIM_API_KEY", "")
        model = os.getenv("NIM_MODEL", "meta/llama-3.1-70b-instruct")
        return backend, base_url, api_key, model

    if backend == "gemini":
        base_url = os.getenv("GEMINI_BASE_URL", _GEMINI_DEFAULT_BASE_URL).rstrip("/") + "/"
        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY", "")
        model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
        return backend, base_url, api_key, model

    if backend == "ollama":
        # 走 Ollama 原生 API（ChatOllama），base_url 不帶 /v1（thinking 才關得掉）
        raw = (os.getenv("OLLAMA_BASE_URL") or os.getenv("OPENAI_BASE_URL", "http://localhost:11434")).rstrip("/")
        if raw.endswith("/v1"):
            raw = raw[:-3].rstrip("/")
        api_key = os.getenv("OPENAI_API_KEY", "ollama")
        model = os.getenv("OPENAI_MODEL", "qwen2.5:7b-instruct")
        return backend, raw, api_key, model

    # vLLM：OpenAI 相容，base_url 補 /v1
    base_url = _normalize_base_url(os.getenv("OPENAI_BASE_URL", "http://localhost:8000/v1"))
    api_key = os.getenv("OPENAI_API_KEY", "EMPTY")
    model = os.getenv("OPENAI_MODEL", "Qwen/Qwen2.5-14B-Instruct")
    return backend, base_url, api_key, model


def get_chat_llm(temperature: float = 0.3, **kwargs) -> BaseChatModel:
    """
    取得指向 LLM 後端的 ChatOpenAI（vLLM / Ollama / Gemini OpenAI 相容端點）。
    支援 OpenAI tool-calling 與 json_schema 結構化輸出。

    預設關閉 thinking 模式（僅 vLLM / Ollama；Gemini 不送這些未知欄位以免被拒）。
    """
    backend, base_url, api_key, model = _resolve_config()
    timeout = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "60"))
    # 這是「輸出」上限。各任務用自己的 env 覆寫：PLANNER_MAX_TOKENS（行程 JSON）、
    # EXTRACT_MAX_TOKENS（研究擷取 JSON）、SUPERVISOR_MAX_TOKENS（小決策）。
    # 預設 2048 僅為未指定時的保守值；窗口大小由 vLLM --max-model-len 決定（目前部署 64K）。
    max_tokens = int(kwargs.pop("max_tokens", os.getenv("LLM_MAX_TOKENS", "2048")))

    # Ollama：用 ChatOllama 走原生 /api/chat，reasoning=False 才能真正關閉 thinking
    # （Ollama 的 OpenAI 相容端點 /v1 會忽略 think:false，導致模型空轉直到逾時）。
    if backend == "ollama":
        from langchain_ollama import ChatOllama

        kwargs.pop("extra_body", None)  # ChatOllama 不吃 extra_body
        return ChatOllama(
            model=model,
            base_url=base_url,
            temperature=temperature,
            num_predict=max_tokens,
            reasoning=False,  # 停用思考輸出
            client_kwargs={"timeout": timeout},
            **kwargs,
        )

    # 關閉 thinking（僅 vLLM 用 chat_template_kwargs；Gemini/NIM 的 OpenAI 端點會拒絕未知欄位）。
    extra_body: dict = {}
    if backend == "vllm":
        extra_body = {"chat_template_kwargs": {"enable_thinking": False}}
    if "extra_body" in kwargs:
        extra_body = {**extra_body, **kwargs.pop("extra_body")}

    return ChatOpenAI(
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=temperature,
        timeout=timeout,
        max_tokens=max_tokens,
        extra_body=extra_body,
        **kwargs,
    )


def call_structured(
    system: str,
    prompt: str,
    schema: Type[T],
    temperature: float = 0.0,
    max_tokens: int | None = None,
) -> T | None:
    """
    呼叫 vLLM 並取得符合 `schema` 的結構化輸出。

    優先用 ChatOpenAI.with_structured_output(schema, method="json_schema")
    （對應 vLLM 的 guided_json，由伺服端強制 JSON schema）。
    若該路徑失敗，fallback 到「純文字輸出 + 容錯 JSON 解析」。
    """
    messages = [("system", system), ("human", prompt)]

    llm_kwargs: dict = {}
    if max_tokens is not None:
        llm_kwargs["max_tokens"] = max_tokens

    effective_max = llm_kwargs.get("max_tokens", os.getenv("LLM_MAX_TOKENS", "2048"))

    # 路徑 1：伺服端 schema 約束（最可靠）
    try:
        llm = get_chat_llm(temperature=temperature, **llm_kwargs)
        structured = llm.with_structured_output(schema, method="json_schema")
        result = structured.invoke(messages)
        if isinstance(result, schema):
            return result
        if isinstance(result, dict):
            return schema.model_validate(result)
    except Exception as e:
        print(
            f"[call_structured] json_schema 路徑失敗（max_tokens={effective_max}），"
            f"改用文字解析 fallback：{e}"
        )

    # 路徑 2：純文字 + 容錯解析
    try:
        llm = get_chat_llm(temperature=temperature, **llm_kwargs)
        raw = llm.invoke(
            [
                ("system", system + "\n\n只輸出一個純 JSON 物件，不要有任何額外文字或 markdown。"),
                ("human", prompt),
            ]
        )
        text = raw.content if hasattr(raw, "content") else str(raw)
        return safe_parse_pydantic(text, schema)
    except Exception as e:
        print(f"[call_structured] fallback 也失敗：{e}")
        return None


def extract_json_from_text(text: str) -> dict | None:
    """從可能夾雜雜訊的 LLM 輸出中抽出 JSON 物件。"""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass

    match = re.search(r"\{[\s\S]*\}", text or "")
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    return None


def safe_parse_pydantic(raw_output: str, model_class: Type[T]) -> T | None:
    """容錯地把 LLM 文字輸出解析成 Pydantic model。"""
    data = extract_json_from_text(raw_output)
    if data is None:
        return None
    try:
        return model_class.model_validate(data)
    except Exception:
        return None
