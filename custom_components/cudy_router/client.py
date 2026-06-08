from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import re
from http.cookies import SimpleCookie
from typing import Any, Optional
from urllib.parse import quote_plus

import aiohttp
from aiohttp import ClientResponseError, ClientSession, TCPConnector
from bs4 import BeautifulSoup

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10


class CudyClient:
    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        use_https: bool = False,
        verify_ssl: bool = True,
        request_timeout: int = DEFAULT_TIMEOUT,
        session: ClientSession | None = None,
    ) -> None:
        self._host = host.rstrip("/")
        self._username = username
        self._password = password
        self._use_https = use_https
        self._verify_ssl = verify_ssl

        self._external_session = session is not None
        self._session: Optional[ClientSession] = session
        self._timeout = aiohttp.ClientTimeout(total=request_timeout)

        self._sysauth: str | None = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------
    @property
    def base_url(self) -> str:
        scheme = "https" if self._use_https else "http"
        return f"{scheme}://{self._host}"

    @property
    def is_authenticated(self) -> bool:
        return bool(self._sysauth)

    @property
    def sysauth(self) -> str | None:
        return self._sysauth

    # ------------------------------------------------------------------
    # Session handling
    # ------------------------------------------------------------------
    async def _ensure_session(self) -> ClientSession:
        if self._session is None or self._session.closed:
            connector = None
            # allow self-signed certs when verify_ssl=False
            if self._use_https and not self._verify_ssl:
                connector = TCPConnector(ssl=False)
            self._session = aiohttp.ClientSession(timeout=self._timeout, connector=connector)
        return self._session

    async def async_close(self) -> None:
        """Close the aiohttp session (called on HA unload)."""
        if self._session and not self._session.closed and not self._external_session:
            await self._session.close()
        self._session = None

    # backwards compat alias
    async def close(self) -> None:
        await self.async_close()

    # ------------------------------------------------------------------
    # Authentication (LuCI)
    # ------------------------------------------------------------------
    async def authenticate(self) -> bool:
        """Authenticate using the LuCI login form and set sysauth cookie."""

        session = await self._ensure_session()

        # try preferred scheme first, then fallback
        schemes = ["https", "http"] if self._use_https else ["http", "https"]

        for scheme in schemes:
            base = f"{scheme}://{self._host}"
            login_url = f"{base}/cgi-bin/luci/"

            headers_get = {
                "User-Agent": "cudy-router-ha-control",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
            headers_post = {
                "User-Agent": "cudy-router-ha-control",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": login_url,
                "Origin": base,
            }

            # 1) GET login page
            try:
                async with session.get(login_url, headers=headers_get, allow_redirects=True) as resp:
                    html = await resp.text()
            except Exception as e:
                _LOGGER.error("GET login page failed (%s): %s", scheme, e)
                continue

            if not html:
                _LOGGER.error("GET login page failed (%s): empty response", scheme)
                continue

            soup = BeautifulSoup(html, "html.parser")

            def extract(name: str) -> str:
                tag = soup.find("input", {"name": name})
                if tag and tag.has_attr("value"):
                    return str(tag["value"])
                # some firmwares put them in <meta>
                meta = soup.find("meta", {"name": name})
                if meta and meta.has_attr("content"):
                    return str(meta["content"])
                return ""

            _csrf = extract("_csrf")
            token = extract("token")
            salt = extract("salt")

            # 2) compute password
            luci_password = self._password
            if salt:
                hashed = hashlib.sha256((self._password + salt).encode()).hexdigest()
                if token:
                    hashed = hashlib.sha256((hashed + token).encode()).hexdigest()
                luci_password = hashed

            # 3) POST credentials
            body: dict[str, Any] = {
                "_csrf": _csrf,
                "token": token,
                "salt": salt,
                "zonename": "UTC",
                "timeclock": str(int(time.time())),
                "luci_language": "en",
                "luci_username": self._username,
                "luci_password": luci_password,
                "submit": "1",
            }
            body = {k: v for k, v in body.items() if v}

            encoded = "&".join(f"{quote_plus(k)}={quote_plus(str(v))}" for k, v in body.items())

            try:
                async with session.post(
                    login_url,
                    headers=headers_post,
                    data=encoded,
                    allow_redirects=False,
                ) as resp:
                    # try Set-Cookie header first
                    set_cookie = resp.headers.getall("Set-Cookie", [])
                    sysauth = self._parse_sysauth_from_headers(set_cookie)
                    if sysauth:
                        self._sysauth = sysauth
                        return True

                    # fallback to cookie jar
                    jar = session.cookie_jar.filter_cookies(base)
                    for key, cookie in jar.items():
                        if key.lower().startswith("sysauth") and cookie.value:
                            self._sysauth = cookie.value
                            return True
            except Exception as e:
                _LOGGER.error("POST login failed (%s): %s", scheme, e)
                continue

        _LOGGER.debug("Authentication failed: no sysauth cookie obtained")
        return False

    @staticmethod
    def _parse_sysauth_from_headers(set_cookie_headers: list[str]) -> str | None:
        for hdr in set_cookie_headers:
            cookie = SimpleCookie()
            cookie.load(hdr)
            for key in ("sysauth", "sysauth_http", "sysauth_https"):
                morsel = cookie.get(key)
                if morsel and morsel.value:
                    return morsel.value
        return None

    async def ensure_authenticated(self) -> None:
        if not self.is_authenticated:
            ok = await self.authenticate()
            if not ok:
                raise RuntimeError("Authentication failed")

    # ------------------------------------------------------------------
    # Generic request helpers
    # ------------------------------------------------------------------
    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: Any = None,
        require_auth: bool = True,
    ) -> Any:
        """Low-level request helper used by get/post and APIs."""

        if not path.startswith("/"):
            path = "/" + path

        if require_auth:
            await self.ensure_authenticated()

        session = await self._ensure_session()
        url = f"{self.base_url}{path}"

        headers: dict[str, str] = {
            "User-Agent": "cudy-router-ha-control",
            "Accept": "*/*",
        }
        if self.sysauth:
            headers["Cookie"] = f"sysauth={self.sysauth}"

        async with session.request(
            method,
            url,
            params=params,
            json=json,
            data=data,
            headers=headers,
        ) as resp:
            if resp.status == 403 and require_auth:
                await self.authenticate()
                headers["Cookie"] = f"sysauth={self.sysauth}" if self.sysauth else ""
                async with session.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    data=data,
                    headers=headers,
                ) as resp2:
                    resp2.raise_for_status()
                    ctype = resp2.headers.get("Content-Type", "")
                    if "application/json" in ctype:
                        return await resp2.json(content_type=None)
                    return await resp2.text()

            try:
                resp.raise_for_status()
            except ClientResponseError:
                return ""

            ctype = resp.headers.get("Content-Type", "")
            if "application/json" in ctype:
                return await resp.json(content_type=None)
            return await resp.text()

    async def get(self, path: str, **kwargs: Any) -> Any:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> Any:
        return await self.request("POST", path, **kwargs)

    async def get_json(self, path: str, **kwargs: Any) -> Any:
        """Explicit JSON helper if a caller wants JSON only."""
        return await self.request("GET", path, **kwargs)

    # ------------------------------------------------------------------
    # LuCI convenience helpers
    # ------------------------------------------------------------------
    async def luci_get(self, luci_path: str) -> str:
        """GET a LuCI path like '/admin/status/overview'."""
        if not luci_path.startswith("/"):
            luci_path = "/" + luci_path
        return await self.get(f"/cgi-bin/luci{luci_path}")

    async def luci_post(self, luci_path: str, data: dict[str, Any] | None = None) -> str:
        """POST to a LuCI path like '/admin/system/reboot'."""
        if not luci_path.startswith("/"):
            luci_path = "/" + luci_path
        return await self.post(f"/cgi-bin/luci{luci_path}", data=data)

    # ------------------------------------------------------------------
    # Cudy servicectl (runtime apply) helpers
    # ------------------------------------------------------------------
    _RE_TOKEN_JS = re.compile(r"token\s*:\s*'([0-9a-fA-F]{16,64})'")

    def _extract_token_anywhere(self, html: str) -> Optional[str]:
        """Try to extract the Cudy CSRF token from HTML/JS."""
        if not html:
            return None
        # 1) classic hidden input
        try:
            soup = BeautifulSoup(html, "html.parser")
            inp = soup.find("input", {"name": "token"})
            if inp and inp.has_attr("value"):
                t = str(inp["value"]).strip()
                if t:
                    return t
        except Exception:
            pass
        # 2) JS snippet: token: '...'
        m = self._RE_TOKEN_JS.search(html)
        if m:
            return m.group(1)
        return None

    async def _servicectl_restart(self, services: str, token: str, *, timeout_s: int = 35) -> bool:
        """Restart services via Cudy OEM endpoint and wait for finish."""
        await self.ensure_authenticated()
        session = await self._ensure_session()

        svc_url = f"{self.base_url}/cgi-bin/luci/admin/servicectl/restart/{services}"
        st_url = f"{self.base_url}/cgi-bin/luci/admin/servicectl/status"

        headers_post = {
            "User-Agent": "cudy-router-ha-control",
            "Accept": "*/*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.base_url}/cgi-bin/luci/admin/setup",
            "Origin": self.base_url,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        headers_get = {
            "User-Agent": "cudy-router-ha-control",
            "Accept": "*/*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.base_url}/cgi-bin/luci/admin/setup",
            "Origin": self.base_url,
        }
        if self.sysauth:
            headers_post["Cookie"] = f"sysauth={self.sysauth}"
            headers_get["Cookie"] = f"sysauth={self.sysauth}"

        # Start restart
        try:
            async with session.post(svc_url, data={"token": token}, headers=headers_post, allow_redirects=True) as r:
                _ = await r.text()
                if r.status >= 400:
                    _LOGGER.debug("servicectl restart %s failed status=%s", services, r.status)
                    return False
        except Exception as e:
            _LOGGER.debug("servicectl restart %s exception: %s", services, e)
            return False

        # Poll status until 'finish'
        deadline = time.time() + float(timeout_s)
        while time.time() < deadline:
            try:
                async with session.get(st_url, headers=headers_get, allow_redirects=True) as r2:
                    txt = (await r2.text()).strip().lower()
                    if txt == "finish":
                        return True
            except Exception:
                pass
            await asyncio.sleep(1)

        _LOGGER.debug("servicectl status timeout for %s", services)
        return False

    # ------------------------------------------------------------------
    # Wi-Fi control (Cudy custom UI / JSON endpoint)
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Wi-Fi control (Cudy LuCI CBI form)
    # ------------------------------------------------------------------
    async def async_set_wifi(self, *args) -> bool:
        """
        Compat:
          - async_set_wifi(enabled)
          - async_set_wifi(band, enabled)   band: '2g'/'5g'/...

        Per gestire 2.4GHz e 5GHz separati (Smart Connect OFF) su Cudy:
          GET  /cgi-bin/luci/admin/network/wireless/config/uncombine?embedded=&nomodal=
          POST /cgi-bin/luci/admin/network/wireless/config/uncombine?embedded=&nomodal=
          multipart/form-data (aiohttp.FormData)

        IMPORTANTISSIMO:
          Il submit salva l'intera pagina -> bisogna inviare SEMPRE sia wlan00.disabled che wlan10.disabled
          preservando l'altra banda.
        """
        # --- parse args ---
        band = "2g"
        if len(args) == 1:
            enabled = bool(args[0])
        elif len(args) == 2:
            band = str(args[0]).lower()
            enabled = bool(args[1])
        else:
            raise TypeError("async_set_wifi expects (enabled) or (band, enabled)")

        if band in ("2.4g", "2g", "24g", "2_4g"):
            band = "2g"
        elif band in ("5g", "5ghz", "5"):
            band = "5g"
        else:
            # default safe
            band = "2g"

        await self.ensure_authenticated()
        session = await self._ensure_session()

        # 1) GET form vero (UNCOMBINE = Smart Connect OFF, con wlan00 + wlan10)
        ts = int(time.time() * 1000)
        get_url = f"{self.base_url}/cgi-bin/luci/admin/network/wireless/config/uncombine?embedded=&nomodal=&_={ts}"

        headers_get = {
            "User-Agent": "cudy-router-ha-control",
            "Accept": "*/*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.base_url}/cgi-bin/luci/admin/setup",
        }
        if self.sysauth:
            headers_get["Cookie"] = f"sysauth={self.sysauth}"

        try:
            async with session.get(get_url, headers=headers_get, allow_redirects=True) as resp:
                status = resp.status
                html = await resp.text()
        except Exception as e:
            _LOGGER.error("Wi-Fi GET uncombine exception: %s", e)
            return False

        if status >= 400 or not html:
            _LOGGER.error("Wi-Fi GET uncombine failed status=%s", status)
            return False

        soup = BeautifulSoup(html, "html.parser")
        form = soup.find("form", {"name": "cbi"}) or soup.find("form")
        if not form:
            _LOGGER.error("Wi-Fi form not found on uncombine head=%r", html[:200])
            return False

        # 2) Build payload completo (come browser)
        payload: dict[str, str] = {}

        for inp in form.find_all("input"):
            name = inp.get("name")
            if not name:
                continue
            itype = (inp.get("type") or "text").lower()

            if itype == "button":
                continue

            if itype in ("checkbox", "radio"):
                if inp.has_attr("checked"):
                    payload[name] = inp.get("value") or "1"
                continue

            payload[name] = inp.get("value") or ""

        for sel in form.find_all("select"):
            name = sel.get("name")
            if not name:
                continue
            opt = sel.find("option", selected=True) or sel.find("option")
            payload[name] = opt.get("value") if opt and opt.has_attr("value") else ""

        for ta in form.find_all("textarea"):
            name = ta.get("name")
            if not name:
                continue
            payload[name] = ta.text or ""

        token = payload.get("token", "")
        if not token:
            _LOGGER.error("Wi-Fi token missing in form payload")
            return False

        # 3) NON inviare campi 'combined' (Smart Connect) se presenti per sbaglio
        # (difesa extra: se li mandi, puoi riattivare Smart Connect)
        for k in list(payload.keys()):
            if k.startswith("cbid.wireless.wlan.") and not (
                k.startswith("cbid.wireless.wlan00.") or k.startswith("cbid.wireless.wlan10.")
            ):
                payload.pop(k, None)
            if k.startswith("cbi.cbe.wireless.wlan.") and not (
                k.startswith("cbi.cbe.wireless.wlan00.") or k.startswith("cbi.cbe.wireless.wlan10.")
            ):
                payload.pop(k, None)

        # 4) Preserva entrambi i flag e modifica solo quello richiesto
        # disabled: 0 = enabled, 1 = disabled
        disabled_val = "0" if enabled else "1"

        cur_2g = payload.get("cbid.wireless.wlan00.disabled", "0")
        cur_5g = payload.get("cbid.wireless.wlan10.disabled", "0")

        payload["cbi.cbe.wireless.wlan00.disabled"] = payload.get("cbi.cbe.wireless.wlan00.disabled", "1")
        payload["cbi.cbe.wireless.wlan10.disabled"] = payload.get("cbi.cbe.wireless.wlan10.disabled", "1")

        if band == "2g":
            payload["cbid.wireless.wlan00.disabled"] = disabled_val
            payload["cbid.wireless.wlan10.disabled"] = cur_5g
        else:  # "5g"
            payload["cbid.wireless.wlan10.disabled"] = disabled_val
            payload["cbid.wireless.wlan00.disabled"] = cur_2g

        # flags submit/apply (come browser)
        payload["cbi.submit"] = payload.get("cbi.submit", "1")
        payload["timeclock"] = payload.get("timeclock", str(int(time.time())))
        payload["cbi.apply"] = payload.get("cbi.apply", "1")

        # 5) POST allo stesso endpoint UNCOMBINE (multipart)
        post_url = f"{self.base_url}/cgi-bin/luci/admin/network/wireless/config/uncombine?embedded=&nomodal="

        headers_post = {
            "User-Agent": "cudy-router-ha-control",
            "Accept": "*/*",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/cgi-bin/luci/admin/setup",
        }
        if self.sysauth:
            headers_post["Cookie"] = f"sysauth={self.sysauth}"

        formdata = aiohttp.FormData()
        for k, v in payload.items():
            formdata.add_field(k, str(v))

        try:
            async with session.post(post_url, headers=headers_post, data=formdata, allow_redirects=True) as resp2:
                body2 = await resp2.text()
                _LOGGER.debug(
                    "Wi-Fi POST uncombine band=%s enabled=%s -> wlan00=%s wlan10=%s status=%s head=%r",
                    band,
                    enabled,
                    payload.get("cbid.wireless.wlan00.disabled"),
                    payload.get("cbid.wireless.wlan10.disabled"),
                    resp2.status,
                    body2[:160],
                )
                if resp2.status >= 400:
                    return False

                # Apply runtime changes like the Cudy UI (restart wireless,vlan)
                tok = self._extract_token_anywhere(body2) or self._extract_token_anywhere(html) or token
                if tok:
                    ok_apply = await self._servicectl_restart('wireless,vlan', tok, timeout_s=35)
                    _LOGGER.debug('Wi-Fi servicectl apply wireless,vlan -> %s', ok_apply)
                else:
                    _LOGGER.debug('Wi-Fi servicectl token not found; skipping servicectl apply')
        except Exception as e:
            _LOGGER.error("Wi-Fi POST uncombine exception: %s", e)
            return False

        return True

    async def async_get_wifi_state(self) -> dict[str, bool]:
        """
        Legge lo stato reale dal router (Smart Connect OFF / uncombine):
          - 2.4G enabled se cbid.wireless.wlan00.disabled == "0"
          - 5G  enabled se cbid.wireless.wlan10.disabled == "0"
        Ritorna: {"2g": True/False, "5g": True/False}
        """
        await self.ensure_authenticated()
        session = await self._ensure_session()

        ts = int(time.time() * 1000)
        url = f"{self.base_url}/cgi-bin/luci/admin/network/wireless/config/uncombine?embedded=&nomodal=&_={ts}"

        headers_get = {
            "User-Agent": "cudy-router-ha-control",
            "Accept": "*/*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.base_url}/cgi-bin/luci/admin/setup",
        }
        if self.sysauth:
            headers_get["Cookie"] = f"sysauth={self.sysauth}"

        async with session.get(url, headers=headers_get, allow_redirects=True) as resp:
            resp.raise_for_status()
            html = await resp.text()

        soup = BeautifulSoup(html, "html.parser")

        def _val(name: str) -> str | None:
            inp = soup.find("input", {"name": name})
            if inp and inp.has_attr("value"):
                return str(inp["value"])
            return None

        v2 = _val("cbid.wireless.wlan00.disabled")
        v5 = _val("cbid.wireless.wlan10.disabled")

        # fallback safe: se non troviamo i campi, non inventiamo stati
        if v2 is None or v5 is None:
            raise RuntimeError("Wi-Fi status fields not found on uncombine page")

        return {
            "2g": (v2 == "0"),
            "5g": (v5 == "0"),
        }



# ------------------------------------------------------------------
# VPN / ZeroTier control (LuCI CBI form)
# ------------------------------------------------------------------
    _VPN_GENERIC_PROTO = "cbid.vpn.config._proto"
    _VPN_GENERIC_ENABLED = "cbid.vpn.config.enabled"
    _VPN_GENERIC_ENABLED_HELPER = "cbi.cbe.vpn.config.enabled"
    _ZEROTIER_DEDICATED_ENABLED = "cbid.zerotier.client.enabled"
    _ZEROTIER_DEDICATED_ENABLED_HELPER = "cbi.cbe.zerotier.client.enabled"
    _ZEROTIER_PROTO_VALUES = {"zerotier", "zerotiers"}
    _WIREGUARD_PROTO_VALUES = {"wireguard", "wireguards"}

    async def _fetch_luci_form(
        self,
        urls: list[str],
        *,
        referer: str,
    ) -> tuple[str, BeautifulSoup, Any, str]:
        await self.ensure_authenticated()
        session = await self._ensure_session()

        headers = {
            "User-Agent": "cudy-router-ha-control",
            "Accept": "*/*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": referer,
        }
        if self.sysauth:
            headers["Cookie"] = f"sysauth={self.sysauth}"

        last_err: Exception | None = None
        for url in urls:
            try:
                async with session.get(url, headers=headers, allow_redirects=True) as resp:
                    html = await resp.text()
                if not html:
                    raise RuntimeError(f"Empty page: {url}")
                soup = BeautifulSoup(html, "html.parser")
                form = soup.find("form", {"name": "cbi"}) or soup.find("form")
                if not form:
                    raise RuntimeError(f"CBI form not found in {url}")
                return html, soup, form, url
            except Exception as e:
                last_err = e
                continue
        raise RuntimeError(f"Unable to fetch LuCI form: {last_err}")

    @staticmethod
    def _form_value(form: Any, name: str) -> str | None:
        inp = form.find("input", {"name": name})
        if inp and inp.has_attr("value"):
            return str(inp["value"])
        sel = form.find("select", {"name": name})
        if sel:
            opt = sel.find("option", selected=True) or sel.find("option")
            if opt and opt.has_attr("value"):
                return str(opt["value"])
        ta = form.find("textarea", {"name": name})
        if ta is not None:
            return ta.text or ""
        return None

    @staticmethod
    def _build_form_fields(form: Any) -> dict[str, str]:
        fields: dict[str, str] = {}
        for el in form.find_all(["input", "select", "textarea"]):
            name = el.get("name")
            if not name:
                continue

            if el.name == "input" and el.get("type") in ("submit", "button", "image", "reset", "password"):
                continue

            if el.name == "select":
                opt = el.find("option", selected=True) or el.find("option")
                if opt and opt.has_attr("value"):
                    fields[name] = str(opt["value"])
                continue

            if el.name == "textarea":
                fields[name] = el.text or ""
                continue

            typ = (el.get("type") or "").lower()
            if typ in ("checkbox", "radio"):
                if el.has_attr("checked"):
                    fields[name] = str(el.get("value") or "1")
                else:
                    fields.setdefault(name, str(el.get("value") or "0"))
            else:
                if el.has_attr("value"):
                    fields[name] = str(el["value"])
        return fields

    async def _post_luci_form(
        self,
        post_url: str,
        fields: dict[str, str],
        *,
        referer: str,
        headers_get_referer: str,
        default_follow_url: str | None = None,
    ) -> tuple[bool, str | None]:
        await self.ensure_authenticated()
        session = await self._ensure_session()

        headers_post = {
            "User-Agent": "cudy-router-ha-control",
            "Accept": "*/*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": referer,
            "Origin": self.base_url,
        }
        headers_get = {
            "User-Agent": "cudy-router-ha-control",
            "Accept": "*/*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": headers_get_referer,
        }
        if self.sysauth:
            cookie = f"sysauth={self.sysauth}"
            headers_post["Cookie"] = cookie
            headers_get["Cookie"] = cookie

        formdata = aiohttp.FormData()
        for k, v in fields.items():
            formdata.add_field(k, str(v))

        async with session.post(post_url, data=formdata, headers=headers_post, allow_redirects=False) as resp:
            body = await resp.text()
            loc = resp.headers.get("Location")
            status = resp.status

        if status not in (200, 302, 303):
            _LOGGER.error("LuCI POST failed url=%s status=%s", post_url, status)
            return False, body

        follow_url = None
        if loc:
            follow_url = f"{self.base_url}{loc}" if not loc.startswith("http") else loc
        elif default_follow_url:
            follow_url = default_follow_url

        if follow_url:
            try:
                async with session.get(follow_url, headers=headers_get, allow_redirects=True) as resp2:
                    body = await resp2.text()
            except Exception as e:
                _LOGGER.debug("Follow apply URL failed (%s): %s", follow_url, e)

        return True, body

    async def async_get_vpn_state(self) -> dict[str, bool]:
        """Ritorna lo stato del WireGuard switch.

        Regola importante:
        - sui firmware con pagina ZeroTier dedicata (es. M3000), se ZeroTier è attivo
          allora WireGuard deve risultare OFF anche se la pagina VPN generica è rimasta
          su un vecchio proto wireguard.
        - sui firmware shared-backend (es. P4), WireGuard è ON solo se enabled=1 e
          il protocollo corrente è wireguard / wireguards.
        """
        ts = int(time.time() * 1000)

        # 1) Priorità alla pagina ZeroTier dedicata: se esiste ed è attiva,
        #    WireGuard deve essere forzato OFF.
        dedicated_urls = [
            f"{self.base_url}/cgi-bin/luci/admin/network/vpn/zerotier?embedded=&mvpn=&_={ts}",
            f"{self.base_url}/cgi-bin/luci/admin/network/vpn/zerotier?_={ts}",
            f"{self.base_url}/cgi-bin/luci/admin/network/vpn/zerotiers?embedded=&mvpn=&_={ts}",
            f"{self.base_url}/cgi-bin/luci/admin/network/vpn/zerotiers?_={ts}",
        ]
        try:
            _, _, zt_form, _ = await self._fetch_luci_form(
                dedicated_urls,
                referer=f"{self.base_url}/cgi-bin/luci/admin/network/vpn/zerotier",
            )
            zt_enabled = self._form_value(zt_form, self._ZEROTIER_DEDICATED_ENABLED)
            if zt_enabled == "1":
                return {"wireguard": False}
        except Exception:
            pass

        # 2) Fallback alla pagina VPN generica.
        urls = [
            f"{self.base_url}/cgi-bin/luci/admin/network/vpn/config?nomodal=&_={ts}",
            f"{self.base_url}/cgi-bin/luci/admin/network/vpn?_={ts}",
        ]

        _, _, form, _ = await self._fetch_luci_form(
            urls,
            referer=f"{self.base_url}/cgi-bin/luci/admin/network/vpn",
        )

        enabled_val = (self._form_value(form, self._VPN_GENERIC_ENABLED) or "").strip()
        proto_val = (self._form_value(form, self._VPN_GENERIC_PROTO) or "").strip().lower()

        if enabled_val != "1":
            return {"wireguard": False}
        if proto_val in self._WIREGUARD_PROTO_VALUES:
            return {"wireguard": True}
        return {"wireguard": False}

    async def async_set_vpn(self, enabled: bool) -> bool:
        """Abilita/disabilita il toggle VPN WireGuard preservando gli altri campi."""
        ts = int(time.time() * 1000)
        get_url = f"{self.base_url}/cgi-bin/luci/admin/network/vpn/config?nomodal=&_={ts}"

        html, _, form, _ = await self._fetch_luci_form(
            [get_url],
            referer=f"{self.base_url}/cgi-bin/luci/admin/setup",
        )

        token_val = self._form_value(form, "token") or self._extract_token_anywhere(html)
        fields = self._build_form_fields(form)
        current_proto = (fields.get(self._VPN_GENERIC_PROTO, "") or "").strip().lower()

        fields["cbi.submit"] = "1"
        fields["cbi.apply"] = fields.get("cbi.apply", "1")
        fields["timeclock"] = str(int(time.time()))
        fields[self._VPN_GENERIC_ENABLED_HELPER] = "1"
        fields[self._VPN_GENERIC_ENABLED] = "1" if enabled else "0"

        if enabled and current_proto not in self._WIREGUARD_PROTO_VALUES:
            if "wireguards" in {opt.get("value") for opt in form.find_all("option") if opt.get("value")}:
                fields[self._VPN_GENERIC_PROTO] = "wireguards"
            else:
                fields[self._VPN_GENERIC_PROTO] = "wireguard"

        ok, _ = await self._post_luci_form(
            f"{self.base_url}/cgi-bin/luci/admin/network/vpn/config?nomodal=",
            fields,
            referer=f"{self.base_url}/cgi-bin/luci/admin/setup",
            headers_get_referer=f"{self.base_url}/cgi-bin/luci/admin/setup",
        )
        if not ok:
            return False

        if token_val:
            await self._servicectl_restart("firewall", token_val)
        return True

    async def async_get_zerotier_state(self) -> dict[str, bool]:
        """Ritorna lo stato ZeroTier.

        Supporta due famiglie di firmware:
        1) pagina dedicata zerotier/client
        2) pagina VPN generica con _proto zerotier/zerotiers
        """
        ts = int(time.time() * 1000)

        dedicated_urls = [
            f"{self.base_url}/cgi-bin/luci/admin/network/vpn/zerotier?embedded=&mvpn=&_={ts}",
            f"{self.base_url}/cgi-bin/luci/admin/network/vpn/zerotier?_={ts}",
            f"{self.base_url}/cgi-bin/luci/admin/network/vpn/zerotiers?embedded=&mvpn=&_={ts}",
            f"{self.base_url}/cgi-bin/luci/admin/network/vpn/zerotiers?_={ts}",
        ]
        try:
            _, _, form, _ = await self._fetch_luci_form(
                dedicated_urls,
                referer=f"{self.base_url}/cgi-bin/luci/admin/network/vpn/zerotier",
            )
            enabled_val = self._form_value(form, self._ZEROTIER_DEDICATED_ENABLED)
            if enabled_val is not None:
                return {"zerotier": enabled_val == "1"}
        except Exception:
            pass

        generic_urls = [
            f"{self.base_url}/cgi-bin/luci/admin/network/vpn/config?nomodal=&_={ts}",
            f"{self.base_url}/cgi-bin/luci/admin/network/vpn?_={ts}",
        ]
        _, _, form, _ = await self._fetch_luci_form(
            generic_urls,
            referer=f"{self.base_url}/cgi-bin/luci/admin/network/vpn",
        )
        enabled_val = self._form_value(form, self._VPN_GENERIC_ENABLED)
        proto_val = (self._form_value(form, self._VPN_GENERIC_PROTO) or "").strip().lower()
        if enabled_val is None:
            raise RuntimeError("ZeroTier generic enabled field not found")
        return {"zerotier": enabled_val == "1" and proto_val in self._ZEROTIER_PROTO_VALUES}

    async def async_set_zerotier(self, enabled: bool) -> bool:
        """Abilita/disabilita ZeroTier sia su firmware dedicati sia su quelli P4 VPN-generic."""
        ts = int(time.time() * 1000)

        # ----- prova 1: pagina dedicata ZeroTier -----
        dedicated_urls = [
            f"{self.base_url}/cgi-bin/luci/admin/network/vpn/zerotier?embedded=&mvpn=&_={ts}",
            f"{self.base_url}/cgi-bin/luci/admin/network/vpn/zerotiers?embedded=&mvpn=&_={ts}",
        ]
        try:
            html, _, form, used_url = await self._fetch_luci_form(
                dedicated_urls,
                referer=f"{self.base_url}/cgi-bin/luci/admin/setup",
            )
            if self._form_value(form, self._ZEROTIER_DEDICATED_ENABLED) is not None:
                token_val = self._form_value(form, "token") or self._extract_token_anywhere(html)
                fields = self._build_form_fields(form)
                fields["cbi.submit"] = "1"
                fields["cbi.apply"] = fields.get("cbi.apply", "1")
                fields["timeclock"] = str(int(time.time()))
                fields[self._ZEROTIER_DEDICATED_ENABLED_HELPER] = "1"
                fields[self._ZEROTIER_DEDICATED_ENABLED] = "1" if enabled else "0"

                clean_url = used_url.split("&_=" )[0] if "&_=" in used_url else used_url.split("?_=")[0]
                default_follow = f"{self.base_url}/cgi-bin/luci/admin/network/vpn/apply/zerotier"
                ok, _ = await self._post_luci_form(
                    clean_url,
                    fields,
                    referer=f"{self.base_url}/cgi-bin/luci/admin/setup",
                    headers_get_referer=f"{self.base_url}/cgi-bin/luci/admin/setup",
                    default_follow_url=default_follow,
                )
                if not ok:
                    return False
                if token_val:
                    await self._servicectl_restart("zerotier,firewall", token_val)
                return True
        except Exception as e:
            _LOGGER.debug("Dedicated ZeroTier flow unavailable, fallback to generic VPN flow: %s", e)

        # ----- prova 2: pagina VPN generica (P4 5G SIM) -----
        generic_get_url = f"{self.base_url}/cgi-bin/luci/admin/network/vpn/config?nomodal=&_={ts}"
        html, _, form, _ = await self._fetch_luci_form(
            [generic_get_url],
            referer=f"{self.base_url}/cgi-bin/luci/admin/setup",
        )
        token_val = self._form_value(form, "token") or self._extract_token_anywhere(html)
        fields = self._build_form_fields(form)

        current_proto = (fields.get(self._VPN_GENERIC_PROTO, "") or "").strip().lower()
        option_values = {opt.get("value") for opt in form.find_all("option") if opt.get("value")}

        fields["cbi.submit"] = "1"
        fields["cbi.apply"] = fields.get("cbi.apply", "1")
        fields["timeclock"] = str(int(time.time()))
        fields[self._VPN_GENERIC_ENABLED_HELPER] = "1"
        fields[self._VPN_GENERIC_ENABLED] = "1" if enabled else "0"

        if current_proto not in self._ZEROTIER_PROTO_VALUES:
            # P4 mostra spesso zerotiers = master e zerotier = slave.
            if "zerotiers" in option_values:
                fields[self._VPN_GENERIC_PROTO] = "zerotiers"
            elif "zerotier" in option_values:
                fields[self._VPN_GENERIC_PROTO] = "zerotier"

        ok, _ = await self._post_luci_form(
            f"{self.base_url}/cgi-bin/luci/admin/network/vpn/config?nomodal=",
            fields,
            referer=f"{self.base_url}/cgi-bin/luci/admin/setup",
            headers_get_referer=f"{self.base_url}/cgi-bin/luci/admin/setup",
        )
        if not ok:
            return False

        if token_val:
            await self._servicectl_restart("zerotier,firewall", token_val)
        return True

    # ------------------------------------------------------------------
    # Helper for tests / convenience
    # ------------------------------------------------------------------
    @classmethod
    def from_entry(cls, entry) -> "CudyClient":
        data = entry.data
        protocol = (data.get("protocol") or "http").lower()
        use_https = protocol == "https"
        return cls(
            host=data.get("host"),
            username=data.get("username"),
            password=data.get("password"),
            use_https=use_https,
        )
