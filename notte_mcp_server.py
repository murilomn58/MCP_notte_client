#!/usr/bin/env python3
"""
notte_mcp_server.py

MCP server (STDIO) com ferramenta `run_notte` que:
- tenta usar NotteProxy.from_country('br') por padrão;
- se `use_mcp_router=True` (ou Notte falhar), tenta descobrir/usar o "MCP router/proxy"
  que você criou no FastCloud;
- aceita discovery por:
   A) ENV MCP_ROUTER_HOSTNAME (hostname ou IP direto)
   B) ENV FASTCLOUD_API_URL + FASTCLOUD_API_TOKEN (opcional) para buscar metadados
- faz validação opcional do IP de saída (via ipinfo.io) e devolve o país detectado.

Env esperadas (defina no painel do FastCloud):
- NOTTE_API_KEY        (obrigatória)
- TARGET_URL           (opcional, padrão: https://shopee.com.br)
- MCP_PROXY_URL        (opcional: explicit proxy URL usado como fallback: socks5://user:pass@IP:PORT)
- MCP_ROUTER_HOSTNAME  (opcional: hostname ou IP do seu roteador MCP no FastCloud)
- FASTCLOUD_API_URL    (opcional: API do FastCloud para discovery)
- FASTCLOUD_API_TOKEN  (opcional: token para a API)
- HEADLESS, BROWSER_TYPE, LOCALE, SKIP_GEO_CHECK (True/False)
"""

import os
import json
import traceback
import socket
from typing import Optional, Any, Dict
from urllib.parse import urlparse

import anyio
import requests  # requests é conveniente; confirme que está disponível no ambiente
from mcp.server import Server
from mcp.server.stdio import stdio_server

from notte_sdk import NotteClient
from notte_sdk.types import NotteProxy

SERVER_NAME = "notte-mcp"

# --------------------------
# Utilitários
# --------------------------
def _str_to_bool(val: Optional[str], default: bool = False) -> bool:
    if val is None:
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "on")

def _make_notte_proxy_from_url(url: Optional[str]) -> Optional[NotteProxy]:
    if not url:
        return None
    url = url.strip()
    try:
        if hasattr(NotteProxy, "from_url"):
            return NotteProxy.from_url(url)
    except Exception:
        pass
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

def _is_ip_outside_brazil(ip_check_url: str = "https://ipinfo.io/json") -> Dict[str, Any]:
    """Consulta ipinfo.io para obter o IP e país atual e retorna o json."""
    try:
        r = requests.get(ip_check_url, timeout=10)
        r.raise_for_status()
        data = r.json()
        return {"ok": True, "data": data}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def _resolve_hostname(hostname: str) -> Optional[str]:
    try:
        return socket.gethostbyname(hostname)
    except Exception:
        return None

# --------------------------
# FastCloud discovery (opcional hook)
# --------------------------
def _discover_mcp_router_via_fastcloud(api_url: str, token: str) -> Optional[str]:
    """
    Hook genérico para buscar o endereço do roteador via API do FastCloud.
    A implementação exata depende da API do FastCloud — substitua a lógica conforme necessário.
    Espera que a API retorne JSON com campo 'router_address' contendo 'socks5://IP:PORT' ou similar.
    """
    if not api_url or not token:
        return None
    try:
        headers = {"Authorization": f"Bearer {token}"}
        r = requests.get(api_url, headers=headers, timeout=8)
        r.raise_for_status()
        payload = r.json()
        # Ex.: payload = {"router_address": "socks5://203.0.113.4:1080"}
        addr = payload.get("router_address") or payload.get("proxy_url")
        if addr:
            return addr
    except Exception:
        return None
    return None

# --------------------------
# Notte runner
# --------------------------
def _run_notte_sync(
    api_key: str,
    target_url: str,
    browser_type: str,
    headless: bool,
    locale: str,
    mcp_proxy_url: Optional[str] = None,
    mcp_router_hostname: Optional[str] = None,
    fastcloud_api_url: Optional[str] = None,
    fastcloud_api_token: Optional[str] = None,
    skip_geo_check: bool = False,
    force_use_mcp_router: bool = False,
) -> Dict[str, Any]:
    """
    Fluxo:
     1) tenta NotteProxy.from_country('br')
     2) se force_use_mcp_router=True -> pula para discovery do MCP router
     3) se Notte falhar ou force=True -> tenta:
          - MCP_PROXY_URL (env explicit)
          - MCP_ROUTER_HOSTNAME (resolve e forma sock5/http)
          - FASTCLOUD API discovery (se fornecida)
     4) opção: valida IP de saída via ipinfo.io (padrão: validar)
    """
    client = NotteClient(api_key=api_key)

    def _session_with_proxies(proxies_obj: Optional[NotteProxy]):
        print(f"▶ Iniciando Notte Session (headless={headless}, browser={browser_type}, locale={locale})")
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

    # 1) Se não for forçar MCP, tenta Notte proxy BR primeiro
    proxies_br = None
    if not force_use_mcp_router:
        try:
            proxies_br = NotteProxy.from_country("br")
        except Exception as e:
            print("⚠️ NotteProxy.from_country('br') falhou:", e)
            proxies_br = None

        if proxies_br is not None:
            try:
                # opcional: set env proxies temporariamente? Notte SDK deve usar o objeto
                answer = _session_with_proxies(proxies_br)
                return {"status": "ok", "route": "notte_proxy_br", "result": answer}
            except Exception:
                print("❌ Execução com NotteProxy BR falhou, continuando para MCP discovery...")
                print(traceback.format_exc())

    # 2) descobrir/usar MCP router
    candidates = []

    # ordem de preferência: explicit MCP_PROXY_URL -> MCP_ROUTER_HOSTNAME -> FASTCLOUD API
    if mcp_proxy_url:
        candidates.append(mcp_proxy_url.strip())

    if mcp_router_hostname:
        # se hostname já vier com scheme/porta, use direto; senão assume socks5 default 1080
        parsed = urlparse(mcp_router_hostname)
        if parsed.scheme and parsed.hostname:
            candidates.append(mcp_router_hostname.strip())
        else:
            # tenta resolver o hostname para IP e montar um socks5 padrão
            resolved = _resolve_hostname(mcp_router_hostname)
            if resolved:
                candidates.append(f"socks5://{resolved}:1080")
            else:
                # last resort, use hostname raw as host (no scheme)
                candidates.append(f"socks5://{mcp_router_hostname}:1080")

    # discovery via API do FastCloud (se disponível)
    if fastcloud_api_url and fastcloud_api_token:
        discovered = _discover_mcp_router_via_fastcloud(fastcloud_api_url, fastcloud_api_token)
        if discovered:
            candidates.append(discovered)

    if not candidates:
        return {
            "status": "error",
            "route": "no_mcp_candidates",
            "error": "Nenhum candidato de MCP router encontrado (MCP_PROXY_URL, MCP_ROUTER_HOSTNAME ou FastCloud API não configurados)."
        }

    # tenta cada candidato
    last_err = None
    for cand in candidates:
        print(f"🔎 Tentando candidato MCP router/proxy: {cand}")
        proxies_obj = _make_notte_proxy_from_url(cand)
        if proxies_obj is None:
            # aplica como variáveis de ambiente e tenta
            _set_env_proxy_vars(cand)

        # opcional: checar IP de saída antes de iniciar (quando não skip_geo_check)
        if not skip_geo_check:
            # define temporariamente env para o processo (já set_env_proxy_vars faz isso)
            geo = _is_ip_outside_brazil()
            if geo.get("ok"):
                country = geo["data"].get("country", "").lower()
                ipaddr = geo["data"].get("ip", "")
                print(f"🌐 IP verificado via ipinfo: {ipaddr} / country={country}")
                # se o país for 'br', ainda assim aceitamos; mas você pediu ip externo — então alertamos
                if country == "br":
                    print("⚠️ O IP detectado pertence ao Brasil; se deseja IP externo, continue para próximo candidato.")
                    last_err = f"candidate {cand} resulted in BR IP {ipaddr}"
                    continue
            else:
                print("⚠️ Falha ao checar IP via ipinfo:", geo.get("error"))

        try:
            answer = _session_with_proxies(proxies_obj)
            return {"status": "ok", "route": "mcp_proxy_used", "candidate": cand, "result": answer}
        except Exception:
            print(f"❌ Falha ao usar candidato {cand}:")
            print(traceback.format_exc())
            last_err = traceback.format_exc()
            continue

    return {"status": "error", "route": "mcp_all_failed", "error": last_err}

# --------------------------
# MCP Server
# --------------------------
server = Server(SERVER_NAME)

@server.tool()
async def run_notte(
    target_url: Optional[str] = None,
    headless: Optional[bool] = None,
    browser_type: Optional[str] = None,
    locale: Optional[str] = None,
    use_mcp_router: Optional[bool] = None,
    skip_geo_check: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    run_notte params:
     - use_mcp_router: True => força usar MCP router (MCP_ROUTER_HOSTNAME/MCP_PROXY_URL/FASTCLOUD API)
     - skip_geo_check: True => pula validação de geolocalização do IP de saída
    """
    api_key = os.getenv("NOTTE_API_KEY", "")
    if not api_key or api_key == "SUA_CHAVE_API_PRO":
        return {"status": "error", "error": "NOTTE_API_KEY não definido."}

    env_target = os.getenv("TARGET_URL", "https://shopee.com.br")
    env_headless = _str_to_bool(os.getenv("HEADLESS", "False"), default=False)
    env_browser = os.getenv("BROWSER_TYPE", "firefox")
    env_locale = os.getenv("LOCALE", "pt-BR")
    mcp_proxy_url = os.getenv("MCP_PROXY_URL", "").strip()
    mcp_router_hostname = os.getenv("MCP_ROUTER_HOSTNAME", "").strip()
    fastcloud_api_url = os.getenv("FASTCLOUD_API_URL", "").strip()
    fastcloud_api_token = os.getenv("FASTCLOUD_API_TOKEN", "").strip()

    final_target = target_url or env_target
    final_headless = env_headless if headless is None else bool(headless)
    final_browser = browser_type or env_browser
    final_locale = locale or env_locale
    final_use_mcp_router = _str_to_bool(os.getenv("FORCE_USE_MCP_ROUTER", "False")) if use_mcp_router is None else bool(use_mcp_router)
    final_skip_geo = _str_to_bool(os.getenv("SKIP_GEO_CHECK", "False")) if skip_geo_check is None else bool(skip_geo_check)

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
        final_use_mcp_router,
    )

@server.tool()
async def health() -> Dict[str, Any]:
    return {
        "server": SERVER_NAME,
        "env": {
            "NOTTE_API_KEY_set": bool(os.getenv("NOTTE_API_KEY")),
            "TARGET_URL": os.getenv("TARGET_URL", "https://shopee.com.br"),
            "MCP_PROXY_URL_set": bool(os.getenv("MCP_PROXY_URL")),
            "MCP_ROUTER_HOSTNAME": os.getenv("MCP_ROUTER_HOSTNAME", ""),
            "FASTCLOUD_API_URL_set": bool(os.getenv("FASTCLOUD_API_URL")),
            "HEADLESS": os.getenv("HEADLESS", "False"),
            "BROWSER_TYPE": os.getenv("BROWSER_TYPE", "firefox"),
            "LOCALE": os.getenv("LOCALE", "pt-BR"),
        }
    }

async def _main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write)

if __name__ == "__main__":
    anyio.run(_main)
