from notte_sdk import NotteClient
from notte_sdk.types import NotteProxy  

# 1. Inicializa o cliente Notte com sua chave API
client = NotteClient(api_key="SUA_CHAVE_API_PRO")

# 2. Define a configuração de proxy específica para o Brasil
#implementar pra chamar o mcp_roteador_proxy caso seja bloqueado durante o uso. chamar o mcp server 
proxies_br = NotteProxy.from_country("br")
print(f"🌍 Usando proxy da Notte configurado para: {proxies_br.country}")

# 3. Cria a sessão com solver de CAPTCHA + proxy do Brasil
try:
    with client.Session(
        solve_captchas=True,        # habilita solver automático de CAPTCHA
        browser_type="firefox",     # requerido para solver funcionar
        headless=False,             # False para ver o navegador, True se quiser em background
        proxies=proxies_br,         # <-- 3. Usa a configuração de proxy do Brasil
        locale="pt-BR"              # define localização Brasil
    ) as session:
        
        agent = client.Agent(session=session, max_steps=5)
        
        task = (
            "Acesse a página, resolva quaisquer CAPTCHAs automaticamente, "
            
        )
        url = "https://shopee.com.br"  # substitua pela URL real alvo

        print("🔍 Iniciando sessão Notte com proxy + solver de CAPTCHA...")
        response = agent.run(task=task, url=url)

        print("\n✅ Execução concluída. Resposta:")
        print(response.answer if hasattr(response, 'answer') else response)

except Exception as e:
    print(f"\n❌ Ocorreu um erro durante a execução: {e}")