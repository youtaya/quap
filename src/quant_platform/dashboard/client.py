"""API-only dashboard transport; no database/provider/Qlib dependencies."""

import httpx


class Client:
    def __init__(self, url, token):
        self.url, self.token = url.rstrip("/"), token

    def request(self, method, path, value=None):
        response = httpx.request(
            method,
            self.url + "/api/v1" + path,
            headers={"Authorization": "Bearer " + self.token},
            json=value,
            timeout=20,
            follow_redirects=False,
        )
        if response.status_code >= 300:
            try:
                message = response.json().get("detail", "Request failed")
            except ValueError:
                message = "Backend unavailable"
            raise ValueError(f"{response.status_code}: {message}")
        return response.json()
