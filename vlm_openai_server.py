from __future__ import annotations

import base64
import io
import os
import re
from pathlib import Path
from typing import Any

import torch

# Avoid cuDNN SDPA execution-plan crash on this server stack.
# The service prioritizes correctness/stability over attention-kernel speed.
try:
    torch.backends.cuda.enable_cudnn_sdp(False)
    torch.backends.cuda.enable_flash_sdp(False)
    torch.backends.cuda.enable_mem_efficient_sdp(False)
    torch.backends.cuda.enable_math_sdp(True)
    print("SDPA backend patched: cudnn/flash/mem_efficient disabled, math enabled.")
except Exception as exc:
    print(f"SDPA backend patch skipped: {exc}")
from fastapi import FastAPI
from pydantic import BaseModel
from PIL import Image
from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration
from qwen_vl_utils import process_vision_info


PROJECT_ROOT = Path(__file__).resolve().parent
MODEL_PATH = Path(
    os.getenv(
        "GEOAGENT_VLM_MODEL_PATH",
        str(PROJECT_ROOT / "models" / "Qwen2.5-VL-7B-Instruct"),
    )
).resolve()
SERVED_MODEL_NAME = "Qwen2.5-VL-7B-Instruct"

app = FastAPI()

print(f"Loading model from {MODEL_PATH}")

if not (MODEL_PATH / "config.json").is_file():
    raise FileNotFoundError(
        f"Existing local VLM is incomplete or missing: {MODEL_PATH}. "
        "This server never downloads replacement weights automatically."
    )

model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
    MODEL_PATH,
    local_files_only=True,
    torch_dtype=torch.bfloat16,
    device_map="auto",
    attn_implementation="eager",
)

processor = AutoProcessor.from_pretrained(
    MODEL_PATH,
    local_files_only=True,
    min_pixels=128 * 28 * 28,
    max_pixels=512 * 28 * 28,
)

print("Model loaded.")


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[dict[str, Any]]
    temperature: float | None = 0
    max_tokens: int | None = 900


def _decode_data_url(data_url: str) -> Image.Image:
    if "," in data_url:
        data_url = data_url.split(",", 1)[1]
    raw = base64.b64decode(data_url)
    return Image.open(io.BytesIO(raw)).convert("RGB")


def _normalize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if isinstance(content, str):
            normalized.append({"role": role, "content": [{"type": "text", "text": content}]})
            continue

        new_content = []
        for item in content:
            if item.get("type") == "text":
                new_content.append({"type": "text", "text": item.get("text", "")})
            elif item.get("type") == "image_url":
                url = item.get("image_url", {}).get("url", "")
                image = _decode_data_url(url)
                new_content.append({"type": "image", "image": image})
        normalized.append({"role": role, "content": new_content})

    return normalized


def _extract_json_or_keep(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if match:
        return match.group(0)
    return cleaned


@app.get("/v1/models")
def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": SERVED_MODEL_NAME,
                "object": "model",
                "owned_by": "local",
            }
        ],
    }


@app.post("/v1/chat/completions")
def chat_completions(req: ChatCompletionRequest):
    messages = _normalize_messages(req.messages)

    text = processor.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=True,
    )

    image_inputs, video_inputs = process_vision_info(messages)

    inputs = processor(
        text=[text],
        images=image_inputs,
        videos=video_inputs,
        padding=True,
        return_tensors="pt",
    )

    inputs = inputs.to(model.device)

    with torch.inference_mode():
        generated_ids = model.generate(
            **inputs,
            max_new_tokens=req.max_tokens or 900,
            do_sample=False,
        )

    generated_ids_trimmed = [
        out_ids[len(in_ids):]
        for in_ids, out_ids in zip(inputs.input_ids, generated_ids)
    ]

    output_text = processor.batch_decode(
        generated_ids_trimmed,
        skip_special_tokens=True,
        clean_up_tokenization_spaces=False,
    )[0]

    output_text = _extract_json_or_keep(output_text)

    return {
        "id": "geoagent-local-vlm",
        "object": "chat.completion",
        "model": SERVED_MODEL_NAME,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": output_text,
                },
                "finish_reason": "stop",
            }
        ],
    }


@app.get("/health")
def health():
    return {"status": "ok", "model": SERVED_MODEL_NAME}
