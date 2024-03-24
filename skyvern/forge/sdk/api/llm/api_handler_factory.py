import json
from typing import Any

import litellm
import openai
import structlog

from skyvern.forge import app
from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry
from skyvern.forge.sdk.api.llm.exceptions import DuplicateCustomLLMProviderError, LLMProviderError
from skyvern.forge.sdk.api.llm.models import LLMAPIHandler
from skyvern.forge.sdk.api.llm.utils import llm_messages_builder, parse_api_response
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.settings_manager import SettingsManager

LOG = structlog.get_logger()


class LLMAPIHandlerFactory:
    _custom_handlers: dict[str, LLMAPIHandler] = {}

    @staticmethod
    def get_llm_api_handler(llm_key: str) -> LLMAPIHandler:
        llm_config = LLMConfigRegistry.get_config(llm_key)

        async def llm_api_handler(
            prompt: str,
            step: Step | None = None,
            screenshots: list[bytes] | None = None,
            parameters: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            if parameters is None:
                parameters = LLMAPIHandlerFactory.get_api_parameters()

            if step:
                await app.ARTIFACT_MANAGER.create_artifact(
                    step=step,
                    artifact_type=ArtifactType.LLM_PROMPT,
                    data=prompt.encode("utf-8"),
                )
                for screenshot in screenshots or []:
                    await app.ARTIFACT_MANAGER.create_artifact(
                        step=step,
                        artifact_type=ArtifactType.SCREENSHOT_LLM,
                        data=screenshot,
                    )

            # TODO (kerem): instead of overriding the screenshots, should we just not take them in the first place?
            if not llm_config.supports_vision:
                screenshots = None

            messages = await llm_messages_builder(prompt, screenshots)
            if step:
                await app.ARTIFACT_MANAGER.create_artifact(
                    step=step,
                    artifact_type=ArtifactType.LLM_REQUEST,
                    data=json.dumps(
                        {
                            "model": llm_config.model_name,
                            "messages": messages,
                            **parameters,
                        }
                    ).encode("utf-8"),
                )
            try:
                # TODO (kerem): add a timeout to this call
                # TODO (kerem): add a retry mechanism to this call (acompletion_with_retries)
                # TODO (kerem): use litellm fallbacks? https://litellm.vercel.app/docs/tutorials/fallbacks#how-does-completion_with_fallbacks-work
                response = await litellm.acompletion(
                    model=llm_config.model_name,
                    messages=messages,
                    **parameters,
                )
            except openai.OpenAIError as e:
                raise LLMProviderError(llm_key) from e
            except Exception as e:
                LOG.exception("LLM request failed unexpectedly", llm_key=llm_key)
                raise LLMProviderError(llm_key) from e
            if step:
                await app.ARTIFACT_MANAGER.create_artifact(
                    step=step,
                    artifact_type=ArtifactType.LLM_RESPONSE,
                    data=response.model_dump_json(indent=2).encode("utf-8"),
                )
                llm_cost = litellm.completion_cost(completion_response=response)
                await app.DATABASE.update_step(
                    task_id=step.task_id,
                    step_id=step.step_id,
                    organization_id=step.organization_id,
                    incremental_cost=llm_cost,
                )
            parsed_response = parse_api_response(response)
            if step:
                await app.ARTIFACT_MANAGER.create_artifact(
                    step=step,
                    artifact_type=ArtifactType.LLM_RESPONSE_PARSED,
                    data=json.dumps(parsed_response, indent=2).encode("utf-8"),
                )
            return parsed_response

        return llm_api_handler

    @staticmethod
    def get_api_parameters() -> dict[str, Any]:
        return {
            "max_tokens": SettingsManager.get_settings().LLM_CONFIG_MAX_TOKENS,
            "temperature": SettingsManager.get_settings().LLM_CONFIG_TEMPERATURE,
        }

    @classmethod
    def register_custom_handler(cls, llm_key: str, handler: LLMAPIHandler) -> None:
        if llm_key in cls._custom_handlers:
            raise DuplicateCustomLLMProviderError(llm_key)
        cls._custom_handlers[llm_key] = handler