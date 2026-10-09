"""Small VK transport preserving raw execute_errors and bounding every HTTP call."""

from __future__ import annotations

import requests


class VKError(RuntimeError):
    def __init__(self, code: int):
        self.code = code
        self.permanent = code not in {1, 6, 9, 10, 13, 29}
        super().__init__(f"VK API error {code}")


class Method:
    def __init__(self, client, name=""):
        self.client, self.name = client, name

    def __getattr__(self, name):
        return Method(self.client, (self.name + "." + name).lstrip("."))

    def __call__(self, **kwargs):
        return self.client.method(self.name, kwargs)


class VKClient:
    def __init__(self, token: str, version="5.199", timeout=30):
        self.token, self.version, self.timeout = token, version, timeout

    def get_api(self):
        return Method(self)

    def method(self, method: str, params: dict, *, raw=False):
        response = requests.post(
            "https://api.vk.com/method/" + method,
            data={**params, "access_token": self.token, "v": self.version},
            timeout=(10, self.timeout),
        )
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise VKError(int(payload["error"].get("error_code", 0)))
        if "response" not in payload:
            raise VKError(0)
        return payload if raw else payload["response"]
