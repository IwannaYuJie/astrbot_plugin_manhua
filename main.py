from __future__ import annotations

import base64
import json
import mimetypes
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from astrbot.api import AstrBotConfig, logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.provider import Provider
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
from astrbot.core.utils.quoted_message.extractor import extract_quoted_message_images

MIN_FRAMES = 1
MAX_FRAMES_FALLBACK = 12


@dataclass(slots=True)
class ImageClientConfig:
    source_label: str
    base_url: str
    api_key: str
    model: str
    generation_endpoint: str
    edit_endpoint: str
    image_size: str
    response_format: str
    timeout_seconds: int
    prefer_edit_for_continuity: bool


@dataclass(slots=True)
class FramePlan:
    index: int
    caption: str
    prompt: str
    source: str


class Main(star.Star):
    """生成连续分镜图片，可选参考图续画。"""

    def __init__(
        self, context: star.Context, config: AstrBotConfig | None = None
    ) -> None:
        super().__init__(context, config=config)
        self.config = config if config is not None else {}

    @filter.command_group("manhua", alias={"mh"})
    def manhua(self, event: AstrMessageEvent) -> None:
        """连续分镜命令。"""

    @manhua.command("help")
    async def manhua_help(self, event: AstrMessageEvent) -> None:
        lines = [
            "连续分镜插件",
            "命令：manhua draw [count] <prompt>",
            "别名：mh draw [count] <prompt>",
            "未填写 count 时使用插件默认值，并受 max_frames 限制。",
            "可以直接上传图片，或回复一张图片作为起始参考图。",
            "示例：",
            "  manhua draw 4 cyberpunk detective chasing a suspect at night",
            "  mh draw 6 a fox spirit crossing four seasons",
        ]
        yield event.plain_result("\n".join(lines))

    @manhua.command("draw")
    async def manhua_draw(self, event: AstrMessageEvent) -> None:
        args = self._extract_draw_args(event.get_message_str())
        requested_count, user_prompt = self._parse_draw_args(args)
        frame_count = max(
            MIN_FRAMES,
            min(self._cfg_max_frames(), requested_count),
        )
        if requested_count != frame_count:
            yield event.plain_result(
                f"帧数已按插件限制调整为 {frame_count}。"
            )

        seed_image_path = await self._extract_seed_image(event)
        if not user_prompt and not seed_image_path:
            yield event.plain_result(
                "缺少提示词和参考图。请使用 `manhua draw [count] <prompt>`，"
                "或直接上传/回复一张图片。"
            )
            return

        story_prompt = user_prompt or self._cfg_str(
            "image_only_prompt",
            "延续同一场景，保持角色、风格与动作连贯。",
        )

        try:
            client_cfg = await self._resolve_client_config(event)
        except Exception as exc:
            yield event.plain_result(f"图片后端配置错误：{exc}")
            return

        yield event.plain_result(
            f"正在使用 {client_cfg.source_label} 准备 {frame_count} 帧分镜..."
        )

        prompt_history: list[str] = []
        temp_dir = self._temp_dir()
        continuity_fallback_notified = False
        previous_frame: Path | None = seed_image_path
        next_index = 1
        frame_retry_attempts = max(0, self._cfg_int("frame_retry_attempts", 2))
        total_attempts = frame_retry_attempts + 1
        skipped_frames: list[int] = []

        if seed_image_path and self._cfg_bool("show_reference_as_first_frame", True):
            reference_plan = FramePlan(
                index=1,
                caption="用户提供的参考图，作为起始帧。",
                prompt=story_prompt,
                source="参考图",
            )
            prompt_history.append(reference_plan.prompt)
            yield event.chain_result(
                self._build_frame_chain(
                    frame_plan=reference_plan,
                    frame_count=frame_count,
                    image_path=seed_image_path,
                    include_prompt=self._cfg_bool("show_generated_prompt", True),
                )
            )
            next_index = 2
            if frame_count == 1:
                return

        try:
            timeout = httpx.Timeout(client_cfg.timeout_seconds)
            async with httpx.AsyncClient(timeout=timeout) as client:
                for frame_index in range(next_index, frame_count + 1):
                    current_reference = previous_frame
                    try:
                        frame_plan = await self._plan_frame(
                            event=event,
                            story_prompt=story_prompt,
                            frame_index=frame_index,
                            frame_count=frame_count,
                            prompt_history=prompt_history,
                            reference_image=current_reference,
                            has_user_reference=seed_image_path is not None,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Frame planner failed on frame %s, fallback to deterministic prompt: %s",
                            frame_index,
                            exc,
                        )
                        frame_plan = self._build_fallback_frame_plan(
                            story_prompt=story_prompt,
                            frame_index=frame_index,
                            frame_count=frame_count,
                            has_reference=current_reference is not None,
                        )

                    use_edit_generation = (
                        current_reference is not None
                        and client_cfg.prefer_edit_for_continuity
                    )
                    image_path: Path | None = None
                    last_error: Exception | None = None
                    for attempt in range(1, total_attempts + 1):
                        try:
                            if use_edit_generation and current_reference is not None:
                                try:
                                    image_path = await self._generate_from_edit(
                                        client=client,
                                        cfg=client_cfg,
                                        prompt=frame_plan.prompt,
                                        reference_image=current_reference,
                                        temp_dir=temp_dir,
                                    )
                                except Exception as exc:
                                    logger.warning(
                                        "Edit endpoint failed on frame %s, fallback to text-only generation: %s",
                                        frame_index,
                                        exc,
                                    )
                                    use_edit_generation = False
                                    if not continuity_fallback_notified:
                                        continuity_fallback_notified = True
                                        yield event.plain_result(
                                            "图片编辑接口调用失败，已回退到仅文本方式继续生成。"
                                        )

                            if image_path is None:
                                image_path = await self._generate_from_text(
                                    client=client,
                                    cfg=client_cfg,
                                    prompt=frame_plan.prompt,
                                    temp_dir=temp_dir,
                                )
                            break
                        except Exception as exc:
                            last_error = exc
                            image_path = None
                            logger.warning(
                                "Frame %s generation failed on attempt %s/%s: %s",
                                frame_index,
                                attempt,
                                total_attempts,
                                exc,
                            )
                            if attempt < total_attempts:
                                yield event.plain_result(
                                    f"第 {frame_index} 帧生成失败，正在重试 "
                                    f"({attempt + 1}/{total_attempts})："
                                    f"{self._summarize_error(exc)}"
                                )

                    if image_path is None:
                        skipped_frames.append(frame_index)
                        yield event.plain_result(
                            f"第 {frame_index} 帧生成失败，已跳过并继续后续帧："
                            f"{self._summarize_error(last_error)}"
                        )
                        continue

                    previous_frame = image_path
                    prompt_history.append(frame_plan.prompt)
                    yield event.chain_result(
                        self._build_frame_chain(
                            frame_plan=frame_plan,
                            frame_count=frame_count,
                            image_path=image_path,
                            include_prompt=self._cfg_bool(
                                "show_generated_prompt", True
                            ),
                        )
                    )
                if skipped_frames:
                    yield event.plain_result(
                        "已跳过失败帧："
                        + "、".join(str(index) for index in skipped_frames)
                    )
        except Exception as exc:
            logger.error("Failed to generate sequential images: %s", exc)
            yield event.plain_result(f"生成失败：{exc}")

    def _build_frame_chain(
        self,
        *,
        frame_plan: FramePlan,
        frame_count: int,
        image_path: Path,
        include_prompt: bool,
    ) -> list[Plain | Image]:
        lines = [f"第 {frame_plan.index}/{frame_count} 帧", frame_plan.caption]
        if include_prompt:
            lines.append(f"提示词：{frame_plan.prompt}")
        lines.append(f"来源：{frame_plan.source}")
        return [
            Plain("\n".join(line for line in lines if line).strip()),
            Image.fromFileSystem(str(image_path)),
        ]

    def _extract_draw_args(self, message_text: str) -> str:
        text = re.sub(r"\s+", " ", (message_text or "").strip())
        prefixes = ("manhua draw", "mh draw")
        for prefix in prefixes:
            if text == prefix:
                return ""
            if text.startswith(prefix + " "):
                return text[len(prefix) :].strip()
        return ""

    def _parse_draw_args(self, args: str) -> tuple[int, str]:
        default_count = self._cfg_int("default_frames", 4)
        if not args:
            return default_count, ""
        first, _, remain = args.partition(" ")
        if first.isdigit():
            return int(first), remain.strip()
        return default_count, args.strip()

    async def _extract_seed_image(self, event: AstrMessageEvent) -> Path | None:
        uploaded_image = await self._extract_uploaded_image(event)
        if uploaded_image is not None:
            return uploaded_image

        if not self._cfg_bool("allow_reply_image_as_reference", True):
            return None

        try:
            quoted_images = await extract_quoted_message_images(event)
        except Exception as exc:
            logger.warning("Failed to resolve quoted reference image: %s", exc)
            return None

        for image_ref in quoted_images:
            resolved = await self._resolve_image_ref_to_path(image_ref)
            if resolved is not None:
                return resolved
        return None

    async def _extract_uploaded_image(self, event: AstrMessageEvent) -> Path | None:
        for component in event.get_messages():
            if isinstance(component, Image):
                try:
                    return Path(await component.convert_to_file_path()).resolve()
                except Exception as exc:
                    logger.warning("Failed to parse uploaded reference image: %s", exc)
                    return None
        return None

    async def _resolve_image_ref_to_path(self, image_ref: str) -> Path | None:
        if not image_ref:
            return None
        try:
            image = Image(file=image_ref)
            return Path(await image.convert_to_file_path()).resolve()
        except Exception as exc:
            logger.warning("Failed to resolve image ref `%s`: %s", image_ref, exc)
            return None

    async def _plan_frame(
        self,
        *,
        event: AstrMessageEvent,
        story_prompt: str,
        frame_index: int,
        frame_count: int,
        prompt_history: list[str],
        reference_image: Path | None,
        has_user_reference: bool,
    ) -> FramePlan:
        if self._cfg_bool("auto_plan_prompts", True):
            planner_provider_id = await self._resolve_prompt_planner_provider_id(event)
            if planner_provider_id:
                try:
                    return await self._plan_frame_with_llm(
                        event=event,
                        planner_provider_id=planner_provider_id,
                        story_prompt=story_prompt,
                        frame_index=frame_index,
                        frame_count=frame_count,
                        prompt_history=prompt_history,
                        reference_image=reference_image,
                        has_user_reference=has_user_reference,
                    )
                except Exception as exc:
                    logger.warning(
                        "Prompt planner failed on frame %s, fallback to deterministic prompt: %s",
                        frame_index,
                        exc,
                    )

        return self._build_fallback_frame_plan(
            story_prompt=story_prompt,
            frame_index=frame_index,
            frame_count=frame_count,
            has_reference=reference_image is not None,
        )

    async def _plan_frame_with_llm(
        self,
        *,
        event: AstrMessageEvent,
        planner_provider_id: str,
        story_prompt: str,
        frame_index: int,
        frame_count: int,
        prompt_history: list[str],
        reference_image: Path | None,
        has_user_reference: bool,
    ) -> FramePlan:
        system_prompt = self._planner_system_prompt()
        prompt = "\n".join(
            [
                "Create the next prompt for a sequential image generation workflow.",
                f"Total frames: {frame_count}",
                f"Current frame: {frame_index}",
                f"Original user request: {story_prompt}",
                f"User supplied starting reference image: {'yes' if has_user_reference else 'no'}",
                "Recent frame prompts (oldest to newest):",
                json.dumps(prompt_history[-3:], ensure_ascii=False),
            ]
        )
        planner_model = self._cfg_str("prompt_planner_model", "") or None

        image_context = [str(reference_image)] if reference_image is not None else []
        if image_context and self._cfg_bool("planner_use_image_context", True):
            try:
                llm_resp = await self.context.llm_generate(
                    chat_provider_id=planner_provider_id,
                    prompt=prompt,
                    image_urls=image_context,
                    system_prompt=system_prompt,
                    model=planner_model,
                )
                return self._parse_frame_plan(
                    text=llm_resp.completion_text,
                    frame_index=frame_index,
                    frame_count=frame_count,
                    story_prompt=story_prompt,
                )
            except Exception as exc:
                logger.warning(
                    "Planner image-context call failed on frame %s, retry without image context: %s",
                    frame_index,
                    exc,
                )

        llm_resp = await self.context.llm_generate(
            chat_provider_id=planner_provider_id,
            prompt=prompt,
            system_prompt=system_prompt,
            model=planner_model,
        )
        return self._parse_frame_plan(
            text=llm_resp.completion_text,
            frame_index=frame_index,
            frame_count=frame_count,
            story_prompt=story_prompt,
        )

    def _planner_system_prompt(self) -> str:
        output_language = self._cfg_str("prompt_output_language", "english").lower()
        language_hint = {
            "english": "Write the prompt and caption in English.",
            "chinese": "Write the prompt and caption in Simplified Chinese.",
            "auto": "Use the same language as the user's request unless English is clearly better.",
        }.get(output_language, "Write the prompt and caption in English.")

        return "\n".join(
            [
                "You are an image prompt planner for a sequential storyboard generator.",
                "Return strict JSON only.",
                'Use this schema: {"caption":"...", "prompt":"..."}',
                language_hint,
                "The caption must be one short sentence.",
                "The prompt must be explicit, visual, and continuity-aware.",
                "Preserve character identity, clothing, color palette, framing continuity, and motion direction.",
                "Avoid markdown, code fences, and explanations outside JSON.",
            ]
        )

    def _parse_frame_plan(
        self,
        *,
        text: str,
        frame_index: int,
        frame_count: int,
        story_prompt: str,
    ) -> FramePlan:
        cleaned = (text or "").strip()
        default_plan = self._build_fallback_frame_plan(
            story_prompt=story_prompt,
            frame_index=frame_index,
            frame_count=frame_count,
            has_reference=frame_index > 1,
        )
        if not cleaned:
            return default_plan

        payload: dict[str, Any] | None = None
        try:
            payload = json.loads(cleaned)
        except Exception:
            payload = self._extract_first_json_object(cleaned)

        if not isinstance(payload, dict):
            return FramePlan(
                index=frame_index,
                caption=default_plan.caption,
                prompt=cleaned,
                source="规划器原始文本",
            )

        caption = str(payload.get("caption", "")).strip() or default_plan.caption
        prompt = str(payload.get("prompt", "")).strip() or default_plan.prompt
        return FramePlan(
            index=frame_index,
            caption=caption,
            prompt=prompt,
            source="LLM 规划",
        )

    def _extract_first_json_object(self, text: str) -> dict[str, Any] | None:
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", text):
            try:
                candidate, _ = decoder.raw_decode(text[match.start() :])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                return candidate
        return None

    def _build_fallback_frame_plan(
        self,
        *,
        story_prompt: str,
        frame_index: int,
        frame_count: int,
        has_reference: bool,
    ) -> FramePlan:
        if frame_index == 1 and not has_reference:
            caption = "根据用户请求生成的开场画面。"
            prompt = (
                f"{story_prompt}\n\n"
                f"生成第 {frame_index}/{frame_count} 帧。"
                "清晰建立主体、视觉风格、光照与环境。"
            )
        else:
            caption = "延续当前分镜，保持角色与场景逻辑一致。"
            prompt = (
                f"{story_prompt}\n\n"
                f"生成第 {frame_index}/{frame_count} 帧。"
                "直接承接上一帧。保持角色身份、服装、镜头语言与色彩一致，"
                "同时加入明确的前进动作或情节推进。"
            )

        return FramePlan(
            index=frame_index,
            caption=caption,
            prompt=prompt,
            source="规则回退",
        )

    async def _resolve_prompt_planner_provider_id(self, event: AstrMessageEvent) -> str:
        planner_provider_id = self._cfg_str("prompt_planner_provider_id", "")
        if planner_provider_id:
            return planner_provider_id
        if not self._cfg_bool("use_current_provider_for_prompt_planner", True):
            return ""
        try:
            return await self.context.get_current_chat_provider_id(
                event.unified_msg_origin
            )
        except Exception:
            return ""

    async def _resolve_client_config(
        self, event: AstrMessageEvent
    ) -> ImageClientConfig:
        mode = self._cfg_str("source_mode", "auto").lower()
        manual_error = None
        astrbot_error = None

        if mode in {"auto", "astrbot_provider"}:
            try:
                return await self._resolve_from_astrbot_provider(event)
            except Exception as exc:
                astrbot_error = exc
                if mode == "astrbot_provider":
                    raise

        if mode in {"auto", "openai_compatible"}:
            try:
                return self._resolve_from_manual_openai()
            except Exception as exc:
                manual_error = exc
                if mode == "openai_compatible":
                    raise

        raise RuntimeError(
            f"没有可用的图片后端。astrbot_provider 错误：{astrbot_error}；"
            f"openai_compatible 错误：{manual_error}"
        )

    async def _resolve_from_astrbot_provider(
        self, event: AstrMessageEvent
    ) -> ImageClientConfig:
        provider_id = self._cfg_str("astrbot_provider_id", "")
        if not provider_id and self._cfg_bool("use_current_provider_when_empty", True):
            try:
                provider_id = await self.context.get_current_chat_provider_id(
                    event.unified_msg_origin
                )
            except Exception:
                provider_id = ""

        if not provider_id:
            provider_inst = self.context.get_using_provider(event.unified_msg_origin)
            provider_id = provider_inst.meta().id if provider_inst else ""

        if not provider_id:
            raise RuntimeError("没有可用的 provider ID。")

        provider = self.context.get_provider_by_id(provider_id)
        if not isinstance(provider, Provider):
            raise RuntimeError(f"Provider `{provider_id}` 不是聊天 provider。")

        provider_cfg = provider.provider_config or {}
        model = (
            self._cfg_str("image_model", "")
            or str(provider_cfg.get("model", provider.get_model() or "")).strip()
        )
        if not model:
            raise RuntimeError(
                f"Provider `{provider_id}` 没有配置模型，请在插件中设置 `image_model`。"
            )

        api_key = ""
        get_current_key = getattr(provider, "get_current_key", None)
        if callable(get_current_key):
            api_key = str(get_current_key() or "").strip()
        if not api_key:
            keys = provider.get_keys()
            api_key = str(keys[0] if keys else "").strip()

        base_url = str(provider_cfg.get("api_base", "")).strip()
        return ImageClientConfig(
            source_label=f"astrbot_provider:{provider_id}",
            base_url=base_url,
            api_key=api_key,
            model=model,
            generation_endpoint=self._cfg_str(
                "generation_endpoint", "/images/generations"
            ),
            edit_endpoint=self._cfg_str("edit_endpoint", "/images/edits"),
            image_size=self._cfg_str("image_size", "1024x1024"),
            response_format=self._cfg_str("response_format", "auto"),
            timeout_seconds=self._cfg_int("timeout_seconds", 120),
            prefer_edit_for_continuity=self._cfg_bool(
                "prefer_edit_for_continuity", True
            ),
        )

    def _resolve_from_manual_openai(self) -> ImageClientConfig:
        model = self._cfg_str("image_model", "")
        if not model:
            raise RuntimeError("openai_compatible 模式下必须填写 `image_model`。")

        return ImageClientConfig(
            source_label="openai_compatible",
            base_url=self._cfg_str("openai_base_url", ""),
            api_key=self._cfg_str("openai_api_key", ""),
            model=model,
            generation_endpoint=self._cfg_str(
                "generation_endpoint", "/images/generations"
            ),
            edit_endpoint=self._cfg_str("edit_endpoint", "/images/edits"),
            image_size=self._cfg_str("image_size", "1024x1024"),
            response_format=self._cfg_str("response_format", "auto"),
            timeout_seconds=self._cfg_int("timeout_seconds", 120),
            prefer_edit_for_continuity=self._cfg_bool(
                "prefer_edit_for_continuity", True
            ),
        )

    async def _generate_from_text(
        self,
        client: httpx.AsyncClient,
        cfg: ImageClientConfig,
        prompt: str,
        temp_dir: Path,
    ) -> Path:
        url = self._build_endpoint(cfg.base_url, cfg.generation_endpoint)
        payload: dict[str, Any] = {
            "model": cfg.model,
            "prompt": prompt,
            "n": 1,
            "size": cfg.image_size,
        }
        if cfg.response_format != "auto":
            payload["response_format"] = cfg.response_format
        data = await self._post_json(client=client, url=url, cfg=cfg, payload=payload)
        return await self._save_response_image(client, cfg, data, temp_dir)

    async def _generate_from_edit(
        self,
        client: httpx.AsyncClient,
        cfg: ImageClientConfig,
        prompt: str,
        reference_image: Path,
        temp_dir: Path,
    ) -> Path:
        url = self._build_endpoint(cfg.base_url, cfg.edit_endpoint)
        fields: dict[str, str] = {
            "model": cfg.model,
            "prompt": prompt,
            "n": "1",
            "size": cfg.image_size,
        }
        if cfg.response_format != "auto":
            fields["response_format"] = cfg.response_format

        mime = mimetypes.guess_type(reference_image.name)[0] or "image/png"
        files = {"image": (reference_image.name, reference_image.read_bytes(), mime)}
        data = await self._post_multipart(
            client=client, url=url, cfg=cfg, data=fields, files=files
        )
        return await self._save_response_image(client, cfg, data, temp_dir)

    async def _post_json(
        self,
        client: httpx.AsyncClient,
        url: str,
        cfg: ImageClientConfig,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        current_payload = dict(payload)
        for attempt in range(2):
            response = await client.post(
                url,
                headers=self._auth_headers(cfg),
                json=current_payload,
            )
            if response.is_success:
                return response.json()
            if (
                attempt == 0
                and "response_format" in current_payload
                and response.status_code in {400, 422}
            ):
                current_payload.pop("response_format", None)
                continue
            raise RuntimeError(
                f"HTTP {response.status_code} on {url}: {response.text[:300]}"
            )
        raise RuntimeError(f"Failed to call {url}.")

    async def _post_multipart(
        self,
        client: httpx.AsyncClient,
        url: str,
        cfg: ImageClientConfig,
        data: dict[str, str],
        files: dict[str, tuple[str, bytes, str]],
    ) -> dict[str, Any]:
        current_data = dict(data)
        for attempt in range(2):
            response = await client.post(
                url,
                headers=self._auth_headers(cfg),
                data=current_data,
                files=files,
            )
            if response.is_success:
                return response.json()
            if (
                attempt == 0
                and "response_format" in current_data
                and response.status_code in {400, 422}
            ):
                current_data.pop("response_format", None)
                continue
            raise RuntimeError(
                f"HTTP {response.status_code} on {url}: {response.text[:300]}"
            )
        raise RuntimeError(f"Failed to call {url}.")

    async def _save_response_image(
        self,
        client: httpx.AsyncClient,
        cfg: ImageClientConfig,
        payload: dict[str, Any],
        temp_dir: Path,
    ) -> Path:
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            raise RuntimeError(f"无效的图片响应数据：{payload}")
        item = data[0]
        if not isinstance(item, dict):
            raise RuntimeError(f"无效的图片数据项：{item}")

        b64_data = item.get("b64_json") or item.get("base64")
        if isinstance(b64_data, str) and b64_data:
            return self._save_base64_image(temp_dir, b64_data)

        image_url = item.get("url") or item.get("image_url")
        if not isinstance(image_url, str) or not image_url:
            raise RuntimeError(f"响应中没有找到图片数据：{item}")
        if image_url.startswith("data:image"):
            return self._save_data_uri_image(temp_dir, image_url)
        return await self._download_image(client, cfg, image_url, temp_dir)

    def _save_base64_image(self, temp_dir: Path, b64_data: str) -> Path:
        if b64_data.startswith("data:image"):
            _, _, b64_data = b64_data.partition(",")
        try:
            raw = base64.b64decode(b64_data)
        except Exception as exc:
            raise RuntimeError(f"无效的 base64 图片数据：{exc}") from exc
        ext = self._guess_image_ext(raw)
        path = temp_dir / f"manhua_{uuid.uuid4().hex}{ext}"
        path.write_bytes(raw)
        return path

    def _save_data_uri_image(self, temp_dir: Path, data_uri: str) -> Path:
        _, _, b64_data = data_uri.partition(",")
        if not b64_data:
            raise RuntimeError("无效的 data URI 图片数据。")
        return self._save_base64_image(temp_dir, b64_data)

    async def _download_image(
        self,
        client: httpx.AsyncClient,
        cfg: ImageClientConfig,
        image_url: str,
        temp_dir: Path,
    ) -> Path:
        response = await client.get(image_url, headers=self._auth_headers(cfg))
        if not response.is_success:
            raise RuntimeError(
                f"下载生成图片失败：HTTP {response.status_code}"
            )
        raw = response.content
        content_type = response.headers.get("content-type", "").split(";")[0].strip()
        ext = mimetypes.guess_extension(content_type) or self._guess_image_ext(raw)
        path = temp_dir / f"manhua_{uuid.uuid4().hex}{ext}"
        path.write_bytes(raw)
        return path

    def _build_endpoint(self, base_url: str, endpoint: str) -> str:
        if endpoint.startswith(("http://", "https://")):
            return endpoint
        normalized_base = (base_url or "").strip().rstrip("/")
        if not normalized_base:
            normalized_base = "https://api.openai.com/v1"
        normalized_endpoint = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        return f"{normalized_base}{normalized_endpoint}"

    def _auth_headers(self, cfg: ImageClientConfig) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if cfg.api_key:
            headers["Authorization"] = f"Bearer {cfg.api_key}"
        return headers

    def _guess_image_ext(self, image_bytes: bytes) -> str:
        if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if image_bytes.startswith(b"\xff\xd8"):
            return ".jpg"
        if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
            return ".webp"
        return ".png"

    def _temp_dir(self) -> Path:
        path = Path(get_astrbot_temp_path()) / "astrbot_plugin_manhua"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _cfg_str(self, key: str, default: str = "") -> str:
        value = self.config.get(key, default)
        if value is None:
            return default
        return str(value).strip()

    def _cfg_int(self, key: str, default: int) -> int:
        value = self.config.get(key, default)
        try:
            return int(value)
        except Exception:
            return default

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                return True
            if lowered in {"0", "false", "no", "off"}:
                return False
        return bool(value)

    def _cfg_max_frames(self) -> int:
        return max(MIN_FRAMES, min(self._cfg_int("max_frames", 6), MAX_FRAMES_FALLBACK))

    def _summarize_error(self, exc: Exception | None) -> str:
        if exc is None:
            return "未知错误"
        text = str(exc).strip() or exc.__class__.__name__
        if len(text) > 120:
            return text[:117] + "..."
        return text
