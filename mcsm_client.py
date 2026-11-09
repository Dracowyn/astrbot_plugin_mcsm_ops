"""Asynchronous, credential-safe access to the MCSManager instance API."""

import asyncio
import json
from typing import Any
from urllib.parse import quote, quote_plus

import aiohttp


class McsmError(Exception):
    """Represent a safe-to-display MCSManager API failure."""


class McsmClient:
    """Access one configured MCSManager instance through GET requests."""

    def __init__(
        self,
        panel_url: str,
        api_key: str,
        daemon_id: str,
        instance_uuid: str,
        timeout: float = 10,
    ) -> None:
        """Store connection settings without opening a network session.

        Args:
            panel_url: Panel base URL without an API path.
            api_key: Credential sent only in the request header.
            daemon_id: Target daemon identifier.
            instance_uuid: Target instance identifier.
            timeout: Total timeout for each request in seconds.
        """
        self._panel_url = panel_url.rstrip("/")
        self._api_key = api_key
        self._daemon_id = daemon_id
        self._instance_uuid = instance_uuid
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def redact(self, message: Any) -> str:
        """Remove the API key and common encoded forms from a message.

        Args:
            message: Potentially unsafe response or error text.

        Returns:
            Text with credential occurrences replaced.
        """
        text = str(message)
        if not self._api_key:
            return text
        variants = {
            self._api_key,
            quote(self._api_key, safe=""),
            quote_plus(self._api_key, safe=""),
            json.dumps(self._api_key, ensure_ascii=False)[1:-1],
            json.dumps(self._api_key, ensure_ascii=True)[1:-1],
            repr(self._api_key)[1:-1],
        }
        for secret in sorted(variants, key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]")
        return text

    async def _request(self, path: str, **query: str) -> Any:
        """Request and validate an API envelope without exposing credentials.

        Args:
            path: API route relative to the panel base URL.
            **query: Route-specific query parameters, never credentials.

        Returns:
            The successful envelope's data field.

        Raises:
            McsmError: The request fails or the response is not successful.
        """
        params = {
            "uuid": self._instance_uuid,
            "daemonId": self._daemon_id,
            **query,
        }
        headers = {
            "x-request-api-key": self._api_key,
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/json; charset=utf-8",
        }
        try:
            if self._session is None:
                self._session = aiohttp.ClientSession(timeout=self._timeout)
            async with self._session.get(
                f"{self._panel_url}{path}",
                params=params,
                headers=headers,
                allow_redirects=False,
            ) as response:
                payload = await response.json(content_type=None)
                http_success = 200 <= response.status < 300
        except asyncio.TimeoutError:
            raise McsmError("MCSManager 请求超时。") from None
        except aiohttp.ClientError:
            raise McsmError("无法连接 MCSManager 面板。") from None
        except (ValueError, TypeError):
            raise McsmError("MCSManager 返回了无效的 JSON 响应。") from None
        if (
            not isinstance(payload, dict)
            or "status" not in payload
            or "data" not in payload
        ):
            raise McsmError("MCSManager 返回了无效的响应格式。") from None
        if not http_success or payload["status"] != 200:
            raise McsmError(self.redact(payload["data"])) from None
        return payload["data"]

    async def detail(self) -> dict[str, Any]:
        """Return the instance detail object.

        Returns:
            Instance details with optional status, info, and process fields.

        Raises:
            McsmError: The request fails or the data is not an object.
        """
        data = await self._request("/api/instance")
        if not isinstance(data, dict):
            raise McsmError("MCSManager 返回了无效的实例详情。") from None
        return data

    async def outputlog(self, size: int) -> str:
        """Return the plain-text console log.

        Args:
            size: Maximum recent log size in kilobytes.

        Returns:
            Raw console text, not a structured log object.

        Raises:
            McsmError: The request fails or the data is not text.
        """
        data = await self._request(
            "/api/protected_instance/outputlog", size=f"{size}KB"
        )
        if not isinstance(data, str):
            raise McsmError("MCSManager 返回了无效的控制台日志。") from None
        return data

    async def send_command(self, command: str) -> Any:
        """Send a command without expecting console output in the response.

        Args:
            command: Console command text.

        Returns:
            The panel's success data, normally True.

        Raises:
            McsmError: The request fails.
        """
        return await self._request("/api/protected_instance/command", command=command)

    async def start(self) -> Any:
        """Start the configured instance.

        Returns:
            The panel's operation acknowledgement.

        Raises:
            McsmError: The request fails.
        """
        return await self._request("/api/protected_instance/open")

    async def stop(self) -> Any:
        """Stop the configured instance.

        Returns:
            The panel's operation acknowledgement.

        Raises:
            McsmError: The request fails.
        """
        return await self._request("/api/protected_instance/stop")

    async def restart(self) -> Any:
        """Restart the configured instance.

        Returns:
            The panel's operation acknowledgement.

        Raises:
            McsmError: The request fails.
        """
        return await self._request("/api/protected_instance/restart")

    async def close(self) -> None:
        """Close the lazily created HTTP session, if one exists."""
        if self._session is not None:
            await self._session.close()
            self._session = None
