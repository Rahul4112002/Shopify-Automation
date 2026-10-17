import json
from urllib import error

import pytest

import ai_titles
from ai_titles import GeminiTitleGenerator, build_user_prompt, extract_response_text, parse_names


def test_parse_names_accepts_plain_and_fenced_json():
    payload = {"items": [{"id": "s1", "name": "Whitney 2 Compartment"}, {"id": "s2", "name": ""}]}
    assert parse_names(json.dumps(payload)) == {"s1": "Whitney 2 Compartment"}
    assert parse_names("```json\n" + json.dumps(payload) + "\n```") == {"s1": "Whitney 2 Compartment"}
    assert parse_names(json.dumps([{"id": "s3", "name": "Glare"}])) == {"s3": "Glare"}


def test_extract_response_text_skips_thought_parts():
    payload = {
        "candidates": [
            {"content": {"parts": [{"text": "thinking", "thought": True}, {"text": '{"items": []}'}]}}
        ]
    }
    assert extract_response_text(payload) == '{"items": []}'


def test_build_user_prompt_lists_every_item():
    prompt = build_user_prompt([{"id": "s1", "product_title": "A"}, {"id": "s2", "product_title": "B"}])
    assert '"id": "s1"' in prompt and '"id": "s2"' in prompt


def test_generator_batches_and_falls_back_to_next_model(monkeypatch):
    generator = GeminiTitleGenerator("key", models=["missing-model", "good-model"], batch_size=2, max_workers=1)
    seen_models = []

    def fake_post(model, prompt):
        seen_models.append(model)
        if model == "missing-model":
            raise error.HTTPError("url", 404, "not found", {}, None)
        ids = [json.loads(line)["id"] for line in prompt.splitlines() if line.startswith("{")]
        return json.dumps({"items": [{"id": item_id, "name": f"Name {item_id}"} for item_id in ids]})

    monkeypatch.setattr(generator, "_post", fake_post)
    items = [{"id": f"s{i}", "product_title": f"P{i}"} for i in range(1, 4)]

    names = generator(items)

    assert names == {"s1": "Name s1", "s2": "Name s2", "s3": "Name s3"}
    assert generator.model_used == "good-model"
    assert seen_models.count("missing-model") == 1  # remembered as unavailable


def test_generator_reports_errors_instead_of_raising(monkeypatch):
    generator = GeminiTitleGenerator("key", models=["m"], max_retries=0)

    def broken(model, prompt):
        raise error.HTTPError("url", 403, "forbidden", {}, None)

    monkeypatch.setattr(generator, "_post", broken)
    assert generator([{"id": "s1"}]) == {}
    assert any("API key" in message for message in generator.describe()["errors"])


def test_api_key_and_model_config(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert ai_titles.build_title_generator() is None

    monkeypatch.setenv("GEMINI_API_KEY", " abc ")
    monkeypatch.setenv("GEMINI_MODEL", "my-model")
    generator = ai_titles.build_title_generator()
    assert generator is not None
    assert generator.models[0] == "my-model"
    assert ai_titles.DEFAULT_MODELS[0] in generator.models


def test_generator_requires_key():
    with pytest.raises(ValueError):
        GeminiTitleGenerator("")


def test_real_http_request_shape_against_local_server(monkeypatch):
    """Exercise the urllib request/response path with a fake Gemini endpoint."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    received = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - http.server API
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received["path"] = self.path
            received["key"] = self.headers.get("x-goog-api-key")
            received["body"] = body
            prompt = body["contents"][0]["parts"][0]["text"]
            ids = [json.loads(line)["id"] for line in prompt.splitlines() if line.startswith("{")]
            answer = {"items": [{"id": item_id, "name": "Whitney 2 Compartment"} for item_id in ids]}
            payload = {"candidates": [{"content": {"parts": [{"text": json.dumps(answer)}]}}]}
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr(
            ai_titles,
            "GEMINI_URL",
            f"http://127.0.0.1:{server.server_port}/v1beta/models/{{model}}:generateContent",
        )
        generator = GeminiTitleGenerator("secret", models=["gemini-test"], max_retries=0)
        names = generator([{"id": "s1", "product_title": "Lavie Signature Whitney 2c Women's Satchel Bag"}])
    finally:
        server.shutdown()

    assert names == {"s1": "Whitney 2 Compartment"}
    assert received["path"] == "/v1beta/models/gemini-test:generateContent"
    assert received["key"] == "secret"
    assert received["body"]["generationConfig"]["responseMimeType"] == "application/json"
    assert "responseSchema" in received["body"]["generationConfig"]
    assert "Whitney" in received["body"]["systemInstruction"]["parts"][0]["text"]
