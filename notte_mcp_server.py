#!/usr/bin/env python3
"""
FastMCP app: notte_mcp_server.py

Expõe duas tools:
- health: checa variáveis de ambiente
- run_notte: roda Notte com solver de CAPTCHA, usando:
   1) NotteProxy.from_country('br') por padrão
   2) Fallback para MCP router/proxy (MCP_PROXY_URL / MCP_ROUTER_HOSTNAME / FASTCLOUD_API_URL+TOKEN)
      quando solicitado (use_mcp_router=True) ou se a rota BR falhar
   3) Checagem opcional do IP de saída (ipinfo.io) para garantir IP externo

ENV esperadas no FastMCP Cloud:
- NOTTE_API_KEY        (obrigatória)
- TARGET_URL           (opcional, padrão: https://shopee.com.br)
- MCP_PROXY_URL        (opcional: ex. socks5://user:pass@IP:PORT ou http://IP:PORT)
- MCP_ROUTER_HOSTNAME  (opcional: hostname/IP do seu roteador MCP)
- FASTCLOUD_API_URL    (opcional: endpoint para discovery)
- FASTCLOUD_API_TOKEN  (opcional: token da API)
- HEADLESS             (True/False, padrão False)
- BROWSER_TYPE         (firefox|chrome, padrão firefox)
- LOCALE               (padrão pt-BR)
- SKIP_GEO_CHECK       (True/False, padrão False)
- FORCE_USE_MCP_ROUTER (True/False, padrão False)
"""

import os
import socket
import traceback
from typing import Optional, Any, Dict
from urllib.parse import urlparse

import anyio
import requests

from fastmcp import FastMCP  # <<<<<< usar FastMCP (não o pacote mcp)
from notte_sdk import NotteClient
from notte_sdk.types import NotteProxy

app = FastMCP("notte-mcp")  # <<<<<< instância que o inspector do FastMCP procura

# --------------------------
# Utils
# --------------------------
def _str_to_bool(val: Optional[str], default: bool = False) -> bool:
    if val is None:
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "on")

def _make_notte_proxy_from_url(url: Optional[str]) -> Optional[NotteProxy]:
    if not url:
        return None
    url = url.strip()
    # tenta NotteProxy.from_url
    try:
        if hasattr(NotteProxy, "from_url"):
            return NotteProxy.from_url(url)
    except Exception:
        pass
    # tenta NotteProxy.from_host_port
    try:
        parsed = urlparse(url)
        scheme = parsed.scheme or "http"
        host = parsed.hostname
        port = parsed.port
        if host and port and hasattr(NotteProxy, "from_host_port"):
            return NotteProxy.from_host_port(host=host, port=port, scheme=scheme)
    except Exception:
        pass
    return None

def _set_env_proxy_vars(url: str) -> None:
    if not url:
        return
    for k in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        os.environ[k] = url

def _resolve_hostname(hostname: str) -> Optional[str]:
    try:
        return socket.gethostbyname(hostname)
    except Exception:
        return None

def _geo_check_ip(ip_check_url: str = "https://ipinfo.io/json") -> Dict[str, Any]:
    try:
        r = requests.get(ip_check_url, timeout=10)
        r.raise_for_status()
        return {"ok": True, "data": r.json()}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def _discover_mcp_router_via_fastcloud(api_url: str, token: str) -> Optional[str]:
    """Hook genérico para discovery via API do FastCloud (ajuste conforme sua conta/endpoint)."""
    if not api_url or not token:
        return None
    try:
        headers = {"Authorization": f"Bearer {token}"}
        r = requests.get(api_url, headers=headers, timeout=8)
        r.raise_for_status()
        payload = r.json()
        # ex.: {"router_address": "socks5://203.0.113.4:1080"} ou {"proxy_url": "..."}
        return payload.get("router_address") or payload.get("proxy_url")
    except Exception:
        return None

# --------------------------
# Core Notte (executa em thread pra não travar o event loop)
# --------------------------
def _run_notte_sync(
    api_key: str,
    target_url: str,
    browser_type: str,
    headless: bool,
    locale: str,
    mcp_proxy_url: Optional[str],
    mcp_router_hostname: Optional[str],
    fastcloud_api_url: Optional[str],
    fastcloud_api_token: Optional[str],
    skip_geo_check: bool,
    force_use_mcp_router: bool,
) -> Dict[str, Any]:

    client = NotteClient(api_key=api_key)

    def _session_with_proxies(proxies_obj: Optional[NotteProxy]):
        with client.Session(
            solve_captchas=True,
            browser_type=browser_type,
            headless=headless,
            proxies=proxies_obj,
            locale=locale,
        ) as session:
            agent = client.Agent(session=session, max_steps=8)
            task = "Acesse a página, resolva quaisquer CAPTCHAs automaticamente e retorne um resumo"
            resp = agent.run(task=task, url=target_url)
            return getattr(resp, "answer", resp)

    # 1) rota Notte BR (a não ser que force MCP)
    if not force_use_mcp_router:
        proxies_br = None
        try:
            proxies_br = NotteProxy.from_country("br")
        except Exception as e:
            proxies_br = None

        if proxies_br is not None:
            try:
                answer = _session_with_proxies(proxies_br)
                return {"status": "ok", "route": "notte_proxy_br", "result": answer}
            except Exception:
                # segue para MCP
                pass

    # 2) MCP candidates: MCP_PROXY_URL -> MCP_ROUTER_HOSTNAME -> FASTCLOUD API
    candidates = []

    if mcp_proxy_url:
        candidates.append(mcp_proxy_url.strip())

    if mcp_router_hostname:
        parsed = urlparse(mcp_router_hostname)
        if parsed.scheme and parsed.hostname:
            candidates.append(mcp_router_hostname.strip())
        else:
            resolved = _resolve_hostname(mcp_router_hostname)
            if resolved:
                candidates.append(f"socks5://{resolved}:1080")
            else:
                candidates.append(f"socks5://{mcp_router_hostname}:1080")

    if fastcloud_api_url and fastcloud_api_token:
        discovered = _discover_mcp_router_via_fastcloud(fastcloud_api_url, fastcloud_api_token)
        if discovered:
            candidates.append(discovered)

    if not candidates:
        return {
            "status": "error",
            "route": "no_mcp_candidates",
            "error": "Sem MCP proxy/router: configure MCP_PROXY_URL, MCP_ROUTER_HOSTNAME ou FASTCLOUD_API_URL/TOKEN."
        }

    last_err = None
    for cand in candidates:
        proxies_obj = _make_notte_proxy_from_url(cand)
        if proxies_obj is None:
            _set_env_proxy_vars(cand)

        if not skip_geo_check:
            geo = _geo_check_ip()
            if geo.get("ok"):
                country = (geo["data"].get("country") or "").lower()
                ipaddr = geo["data"].get("ip", "")
                # Se ainda for BR e você quer IP externo, tenta o próximo
                if country == "br":
                    last_err = f"candidate {cand} resulted in BR IP {ipaddr}"
                    continue

        try:
            answer = _session_with_proxies(proxies_obj)
            return {"status": "ok", "route": "mcp_proxy_used", "candidate": cand, "result": answer}
        except Exception:
            last_err = traceback.format_exc()
            continue

    return {"status": "error", "route": "mcp_all_failed", "error": last_err}

# --------------------------
# Tools FastMCP
# --------------------------
@app.tool()
async def health() -> Dict[str, Any]:
    """Retorna estado e envs principais para diagnóstico."""
    return {
        "server": "notte-mcp (fastmcp)",
        "env": {
            "NOTTE_API_KEY_set": bool(os.getenv("NOTTE_API_KEY")),
            "TARGET_URL": os.getenv("TARGET_URL", "https://shopee.com.br"),
            "MCP_PROXY_URL_set": bool(os.getenv("MCP_PROXY_URL")),
            "MCP_ROUTER_HOSTNAME": os.getenv("MCP_ROUTER_HOSTNAME", ""),
            "FASTCLOUD_API_URL_set": bool(os.getenv("FASTCLOUD_API_URL")),
            "HEADLESS": os.getenv("HEADLESS", "False"),
            "BROWSER_TYPE": os.getenv("BROWSER_TYPE", "firefox"),
            "LOCALE": os.getenv("LOCALE", "pt-BR"),
            "FORCE_USE_MCP_ROUTER": os.getenv("FORCE_USE_MCP_ROUTER", "False"),
            "SKIP_GEO_CHECK": os.getenv("SKIP_GEO_CHECK", "False"),
        }
    }

@app.tool()
async def run_notte(
    target_url: Optional[str] = None,
    headless: Optional[bool] = None,
    browser_type: Optional[str] = None,
    locale: Optional[str] = None,
    use_mcp_router: Optional[bool] = None,
    skip_geo_check: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    Executa Notte. Parâmetros (opcionais) sobrepõem ENV:
      - target_url
      - headless (True/False)
      - browser_type ('firefox'|'chrome')
      - locale (ex. 'pt-BR')
      - use_mcp_router (force fallback para MCP router/proxy)
      - skip_geo_check (pula validação do IP de saída)
    """
    api_key = os.getenv("NOTTE_API_KEY", "")
    if not api_key or api_key == "SUA_CHAVE_API_PRO":
        return {"status": "error", "error": "NOTTE_API_KEY não definido nas variáveis de ambiente."}

    env_target = os.getenv("TARGET_URL", "https://shopee.com.br")
    env_headless = _str_to_bool(os.getenv("HEADLESS", "False"), default=False)
    env_browser = os.getenv("BROWSER_TYPE", "firefox")
    env_locale = os.getenv("LOCALE", "pt-BR")
    env_force_mcp = _str_to_bool(os.getenv("FORCE_USE_MCP_ROUTER", "False"))
    env_skip_geo = _str_to_bool(os.getenv("SKIP_GEO_CHECK", "False"))

    mcp_proxy_url = os.getenv("MCP_PROXY_URL", "").strip()
    mcp_router_hostname = os.getenv("MCP_ROUTER_HOSTNAME", "").strip()
    fastcloud_api_url = os.getenv("FASTCLOUD_API_URL", "").strip()
    fastcloud_api_token = os.getenv("FASTCLOUD_API_TOKEN", "").strip()

    final_target = target_url or env_target
    final_headless = env_headless if headless is None else bool(headless)
    final_browser = browser_type or env_browser
    final_locale = locale or env_locale
    final_use_mcp = env_force_mcp if use_mcp_router is None else bool(use_mcp_router)
    final_skip_geo = env_skip_geo if skip_geo_check is None else bool(skip_geo_check)

    return await anyio.to_thread.run_sync(
        _run_notte_sync,
        api_key,
        final_target,
        final_browser,
        final_headless,
        final_locale,
        mcp_proxy_url,
        mcp_router_hostname,
        fastcloud_api_url,
        fastcloud_api_token,
        final_skip_geo,
        final_use_mcp,
    )

# Importante: no FastMCP Cloud normalmente basta exportar `app`.
# Mas deixar um main ajuda localmente:
if __name__ == "__main__":
    app.run()
