"""The Interactions request the Google adapter builds from a provider request."""
from __future__ import annotations

import base64
import copy
import json
import unittest

from tess_core.agent.request_integrity import normalize_provider_request_messages
from tess_core.providers.anthropic.payloads import normalize_anthropic_content_blocks
from tess_core.providers.google import (
    build_interactions_create_params,
    convert_messages_to_steps,
    resolve_google_base_url,
    sanitize_google_schema,
)
from tess_core.providers.openai import convert_messages_to_chat_completions, convert_messages_to_input

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()
THOUGHT_A = {"type": "thought", "signature": "sig-A==", "summary": [{"type": "text", "text": "Plan the lookup."}]}
THOUGHT_B = {"type": "thought", "signature": "sig-B=="}
THOUGHT_C = {"type": "thought", "signature": "sig-C=="}

TOOL = {"name": "get_weather", "description": "Weather for a place.", "input_schema": {"type": "object", "properties": {"location": {"type": "string"}}, "required": ["location"]}}


def _conversation():
    return [
        {"role": "user", "content": "What is the weather in Paris?"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Let me check.", "provider_metadata": {"google_thoughts": [THOUGHT_A]}},
            {"type": "tool_use", "id": "call_1", "name": "get_weather", "input": {"location": "Paris"}, "provider_metadata": {"google_thoughts": [THOUGHT_B], "google_trailing_thoughts": [THOUGHT_C]}},
        ]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "Sunny, 22C"}]},
    ]


class GoogleStepConversionTests(unittest.TestCase):
    def test_a_tool_exchange_becomes_steps_in_order_with_thoughts_where_they_were(self):
        steps = convert_messages_to_steps(_conversation())
        self.assertEqual(steps, [
            {"type": "user_input", "content": [{"type": "text", "text": "What is the weather in Paris?"}]},
            THOUGHT_A,
            {"type": "model_output", "content": [{"type": "text", "text": "Let me check."}]},
            THOUGHT_B,
            {"type": "function_call", "id": "call_1", "name": "get_weather", "arguments": {"location": "Paris"}},
            THOUGHT_C,
            {"type": "function_result", "name": "get_weather", "call_id": "call_1", "result": [{"type": "text", "text": "Sunny, 22C"}]},
        ])

    def test_thought_steps_go_back_byte_for_byte_and_the_session_is_not_touched(self):
        messages = _conversation()
        before = copy.deepcopy(messages)
        steps = convert_messages_to_steps(messages)
        self.assertEqual(json.dumps(steps[1], sort_keys=True), json.dumps(THOUGHT_A, sort_keys=True))
        steps[1]["signature"] = "tampered"
        self.assertEqual(messages, before)

    def test_each_request_of_a_tool_loop_extends_the_one_before_it(self):
        messages = _conversation()
        first = build_interactions_create_params({"model": "gemini-4-hydrogen", "system": "Be brief.", "tools": [TOOL], "messages": messages[:1], "maxOutputTokens": 4096})
        second = build_interactions_create_params({"model": "gemini-4-hydrogen", "system": "Be brief.", "tools": [TOOL], "messages": messages, "maxOutputTokens": 4096})
        third = build_interactions_create_params({"model": "gemini-4-hydrogen", "system": "Be brief.", "tools": [TOOL], "messages": [*messages, {"role": "assistant", "content": [{"type": "text", "text": "Sunny."}]}, {"role": "user", "content": "And Rome?"}], "maxOutputTokens": 4096})
        for earlier, later in ((first, second), (second, third)):
            self.assertEqual(later["input"][: len(earlier["input"])], earlier["input"])
            self.assertGreater(len(later["input"]), len(earlier["input"]))
            for key in ("model", "system_instruction", "tools", "generation_config", "store", "stream"):
                self.assertEqual(json.dumps(later[key]), json.dumps(earlier[key]))
            # The serialised input of the earlier call is a literal prefix of the later one.
            prefix = json.dumps(earlier["input"], separators=(",", ":"))[:-1]
            self.assertTrue(json.dumps(later["input"], separators=(",", ":")).startswith(prefix))
        self.assertEqual(json.dumps(second), json.dumps(build_interactions_create_params({"model": "gemini-4-hydrogen", "system": "Be brief.", "tools": [TOOL], "messages": _conversation(), "maxOutputTokens": 4096})))

    def test_an_image_a_tool_returned_goes_inside_its_result(self):
        steps = convert_messages_to_steps([
            {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "Read", "input": {"path": "x.png"}}, {"type": "tool_use", "id": "b", "name": "Read", "input": {"path": "y.png"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "a", "content": "first"},
                {"type": "tool_result", "tool_use_id": "b", "content": "second", "is_error": True},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG}},
                {"type": "text", "text": "A note from the host."},
            ]},
        ])
        self.assertEqual([step["type"] for step in steps], ["function_call", "function_call", "function_result", "function_result", "user_input"])
        self.assertEqual(steps[2]["result"], [{"type": "text", "text": "first"}])
        self.assertEqual(steps[3]["result"], [{"type": "text", "text": "second"}, {"type": "image", "mime_type": "image/png", "data": PNG}])
        self.assertIs(steps[3]["is_error"], True)
        self.assertNotIn("is_error", steps[2])
        self.assertEqual(steps[4]["content"], [{"type": "text", "text": "A note from the host."}])

    def test_a_tool_that_printed_nothing_answers_with_the_empty_string(self):
        # Gemini refuses a text item with no text (400), and accepts the empty string.
        calls = {"role": "assistant", "content": [{"type": "tool_use", "id": name, "name": "Bash", "input": {}} for name in ("a", "b", "c", "d")]}
        steps = convert_messages_to_steps([calls, {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "a", "content": ""},
            {"type": "tool_result", "tool_use_id": "b", "content": None},
            {"type": "tool_result", "tool_use_id": "c", "content": [{"type": "text", "text": ""}, {"type": "text", "text": "kept"}]},
            {"type": "tool_result", "tool_use_id": "d", "content": ""},
            {"type": "image", "source": {"media_type": "image/png", "data": PNG}},
        ]}])
        results = [step["result"] for step in steps if step["type"] == "function_result"]
        self.assertEqual(results, ["", "", [{"type": "text", "text": "kept"}], [{"type": "image", "mime_type": "image/png", "data": PNG}]])

    def test_a_user_image_is_content_and_a_broken_one_is_left_out(self):
        steps = convert_messages_to_steps([{"role": "user", "content": [
            {"type": "text", "text": "Look."},
            {"type": "image", "source": {"media_type": "image/webp", "data": PNG}},
            {"type": "image", "source": {"media_type": "image/png", "data": "not base64!"}},
            {"type": "input_image", "data": f"data:image/jpeg;base64,{PNG}"},
        ]}])
        self.assertEqual(steps, [{"type": "user_input", "content": [
            {"type": "text", "text": "Look."},
            {"type": "image", "mime_type": "image/webp", "data": PNG},
            {"type": "image", "mime_type": "image/jpeg", "data": PNG},
        ]}])

    def test_another_providers_reasoning_is_dropped_and_empty_turns_vanish(self):
        steps = convert_messages_to_steps([
            {"role": "user", "content": ""},
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "private", "signature": "anthropic-sig"}, {"type": "redacted_thinking", "data": "x"}, {"type": "text", "text": ""}]},
            {"role": "assistant", "content": "Plain reply."},
            {"role": "assistant", "content": [{"type": "text", "text": "ok", "provider_metadata": {"reasoning_content": "openai chat reasoning", "google_thoughts": ["junk", {"type": "model_output"}]}}]},
        ])
        self.assertEqual(steps, [
            {"type": "model_output", "content": [{"type": "text", "text": "Plain reply."}]},
            {"type": "model_output", "content": [{"type": "text", "text": "ok"}]},
        ])

    def test_the_other_providers_never_see_google_thoughts(self):
        messages = _conversation()
        anthropic = normalize_anthropic_content_blocks(messages[1]["content"])
        self.assertEqual(anthropic, [{"type": "text", "text": "Let me check."}, {"type": "tool_use", "id": "call_1", "name": "get_weather", "input": {"location": "Paris"}}])
        for rendered in (convert_messages_to_chat_completions("", messages), convert_messages_to_input(messages)):
            self.assertNotIn("sig-", json.dumps(rendered))

    def test_request_normalisation_keeps_the_thoughts_and_repairs_a_missing_result(self):
        messages = _conversation()[:2]
        normalized = normalize_provider_request_messages("google", messages)
        steps = convert_messages_to_steps(normalized["messages"])
        self.assertEqual([step["type"] for step in steps], ["user_input", "thought", "model_output", "thought", "function_call", "thought", "function_result"])
        self.assertEqual(steps[-1]["call_id"], "call_1")
        self.assertEqual(normalized["repair"]["syntheticToolResults"], 1)


class GoogleRequestParamsTests(unittest.TestCase):
    def _params(self, **request):
        base = {"model": "gemini-4-hydrogen", "system": "  Be brief.  ", "tools": [TOOL], "messages": [{"role": "user", "content": "hi"}], "maxOutputTokens": 8000}
        base.update(request)
        return build_interactions_create_params(base, {"requestOptions": request.get("_options") or {}})

    def test_every_request_is_stateless_and_streamed(self):
        params = self._params(effort="xhigh")
        self.assertIs(params["store"], False)
        self.assertIs(params["stream"], True)
        self.assertNotIn("previous_interaction_id", params)
        self.assertEqual(list(params), ["model", "system_instruction", "tools", "input", "generation_config", "stream", "store"])
        self.assertEqual(params["system_instruction"], "Be brief.")
        self.assertEqual(params["tools"], [{"type": "function", "name": "get_weather", "description": "Weather for a place.", "parameters": TOOL["input_schema"]}])
        self.assertEqual(params["generation_config"], {"thinking_level": "xhigh", "thinking_summaries": "auto", "max_output_tokens": 8000})

    def test_a_thinking_level_is_sent_only_when_the_model_takes_it(self):
        self.assertEqual(self._params(model="gemini-4-carbon", effort="xhigh")["generation_config"]["thinking_level"], "high")
        self.assertNotIn("thinking_level", self._params(effort=None)["generation_config"])
        unknown = self._params(model="gemini-9-unknown", effort="high")["generation_config"]
        self.assertEqual(unknown, {"max_output_tokens": 8000})

    def test_no_system_and_no_tools_are_left_out(self):
        params = self._params(system="", tools=[])
        self.assertNotIn("system_instruction", params)
        self.assertNotIn("tools", params)

    def test_a_profile_may_tune_generation_but_not_storage_or_history(self):
        options = {
            "generation_config": {"thinking_summaries": "none", "temperature": 0.2, "max_output_tokens": 5},
            "service_tier": "flex", "store": True, "stream": False, "previous_interaction_id": "v1_x", "input": "replaced", "tools": [], "background": True,
        }
        params = self._params(_options=options, effort="low")
        self.assertEqual(params["generation_config"], {"thinking_level": "low", "thinking_summaries": "none", "temperature": 0.2, "max_output_tokens": 8000})
        self.assertEqual(params["service_tier"], "flex")
        self.assertIs(params["store"], False)
        self.assertIs(params["stream"], True)
        self.assertEqual(len(params["tools"]), 1)
        self.assertEqual(params["input"][0]["type"], "user_input")
        for forbidden in ("previous_interaction_id", "background"):
            self.assertNotIn(forbidden, params)

    def test_a_tool_schema_is_an_object_schema_without_document_annotations(self):
        schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "x", "type": "object", "properties": {"$id": {"type": "string", "$comment": "kept name, dropped note"}, "tags": {"type": "array", "items": {"type": "string", "$comment": "n"}}}, "$defs": {"node": {"type": "object", "properties": {"next": {"$ref": "#/$defs/node"}}}}}
        cleaned = sanitize_google_schema(schema)
        self.assertEqual(cleaned, {"type": "object", "properties": {"$id": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}}, "$defs": {"node": {"type": "object", "properties": {"next": {"$ref": "#/$defs/node"}}}}})
        self.assertEqual(sanitize_google_schema({}), {"type": "object", "properties": {}})
        self.assertEqual(sanitize_google_schema(None), {"type": "object", "properties": {}})
        self.assertIn("$schema", schema)

    def test_base_url(self):
        self.assertEqual(resolve_google_base_url(None), "https://generativelanguage.googleapis.com/v1beta")
        self.assertEqual(resolve_google_base_url(" https://proxy.invalid/v1beta/ "), "https://proxy.invalid/v1beta")
