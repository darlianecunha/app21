# -*- coding: utf-8 -*-
"""
Monitor de Promoções de Transferência de Milhas (últimos 3 dias)
-----------------------------------------------------------------
• Fontes (RSS): Passageiro de Primeira, Melhores Destinos, Pontos pra Voar, Império das Milhas
• Fontes (HTML fallback): páginas de promoções da Smiles e LATAM Pass (quando o RSS não cobre)
• Filtro por palavras-chave (customizável)
• Envio por e-mail via Gmail (senha de APP)
• Notificação opcional via Telegram (se variáveis estiverem configuradas)
• Log em CSV no Google Drive (opcional)

Como usar no Google Colab
-------------------------
1) !pip install feedparser beautifulsoup4 html5lib python-dateutil requests
2) Monte o Google Drive (opcional para salvar logs):
   from google.colab import drive
   drive.mount('/content/drive')
3) Crie um arquivo "credenciais.txt" no seu Drive com 3 linhas:
   <seu_email_gmail>
   <sua_senha_de_app>
   <destinatario>
4) (Opcional) Defina variáveis de Telegram no ambiente:
   TELEGRAM_BOT_TOKEN="123:ABC" e TELEGRAM_CHAT_ID="-100123..."
5) Execute:
   !python monitor_promos_milhas.py
"""

import re
import os
import csv
import ssl
import time
import smtplib
import logging
import traceback
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# Dependências web
try:
    import feedparser
except ImportError:
    raise SystemExit("Instale o feedparser: pip install feedparser")

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    raise SystemExit("Instale requests e beautifulsoup4: pip install requests beautifulsoup4 html5lib")

# ------------------------------
# Configurações
# ------------------------------
TZ_FORTALEZA = timezone(timedelta(hours=-3))  # America/Fortaleza (fixo, sem DST)
AGORA = datetime.now(TZ_FORTALEZA)

# Palavras de busca (ajuste à vontade)
KEYWORDS = [
    # gatilhos gerais
    "transferência", "transferencia", "transferir", "bônus", "bonus", "bonificação", "bonificacao",
    "campanha", "promoção", "promocao", "promo",
    # programas & parceiros comuns no BR
    "smiles", "latam pass", "esfera", "livelo", "tudoazul", "ame", "inter", "santander",
    "bradesco", "itau", "bb", "caixa", "shell box",
    # frases úteis
    "bônus na transferência", "bonus na transferencia", "bonus de transferência", "bonus de transferencia",
]

# Dias de recência (padrão = 3)
RECENCIA_DIAS = 3

# Fontes RSS (preferidas, mais estáveis)
RSS_SOURCES = {
    "Passageiro de Primeira": "https://passageirodeprimeira.com/feed/",
    "Melhores Destinos": "https://www.melhoresdestinos.com.br/feed",
    "Pontos pra Voar": "https://www.pontospravoar.com/feed",
    "Império das Milhas (geral)": "https://imperiodasmilhas.com/feed/",
    "Império das Milhas (promoções)": "https://imperiodasmilhas.com/categoria/promocoes/feed/",
}

# Fontes HTML (fallback simples). URLs podem mudar ao longo do tempo.
HTML_SOURCES = {
    "Smiles - Promoções": "https://www.smiles.com.br/promocoes",
    "LATAM Pass - Promoções": "https://latampass.latam.com/pt_br/promocoes",
}

# Caminhos padrão (pode alterar)
PADRAO_DRIVE_PATH = "/content/drive/MyDrive"  # se estiver no Colab com Drive
ARQ_CREDENCIAIS = os.path.join(PADRAO_DRIVE_PATH, "credenciais.txt")
ARQ_LOG_CSV = os.path.join(PADRAO_DRIVE_PATH, "monitor_promos_milhas_log.csv")

# Telegram (opcional via variáveis de ambiente)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# ------------------------------
# Utilitários
# ------------------------------
def normalizar_texto(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").strip()).lower()

def contem_keywords(texto: str, keywords=KEYWORDS) -> bool:
    nt = normalizar_texto(texto)
    return any(k.lower() in nt for k in keywords)

def dentro_da_janela(data_pub: datetime, dias: int = RECENCIA_DIAS) -> bool:
    if not isinstance(data_pub, datetime):
        return False
    limite = AGORA - timedelta(days=dias)
    return data_pub >= limite

def tentar_parse_datetime(entry):
    # Tenta pegar a data do item RSS de várias formas
    for campo in ("published_parsed", "updated_parsed", "created_parsed"):
        dt = getattr(entry, campo, None) or entry.get(campo)
        if dt:
            try:
                d = datetime.fromtimestamp(time.mktime(dt), tz=timezone.utc).astimezone(TZ_FORTALEZA)
                return d
            except Exception:
                pass
    for campo in ("published", "updated", "created"):
        dt_str = getattr(entry, campo, None) or entry.get(campo)
        if dt_str:
            try:
                from email.utils import parsedate_to_datetime
                d = parsedate_to_datetime(dt_str).astimezone(TZ_FORTALEZA)
                return d
            except Exception:
                pass
    return None

def http_get(url: str, timeout: int = 20):
    try:
        requests.packages.urllib3.disable_warnings()
        resp = requests.get(url, timeout=timeout, verify=False, headers={"User-Agent": "Mozilla/5.0 (PromoBot)"})
        if resp.status_code == 200 and resp.text:
            return resp
    except Exception:
        logging.warning("Falha ao baixar %s\n%s", url, traceback.format_exc())
    return None

def parse_feed_via_requests(url: str):
    headers = {"User-Agent": "Mozilla/5.0 (PromoBot)"}
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    return feedparser.parse(resp.content)

# ------------------------------
# Coleta via RSS
# ------------------------------
def coletar_via_rss(keywords=KEYWORDS, dias=RECENCIA_DIAS):
    resultados = []
    for fonte, url in RSS_SOURCES.items():
        try:
            feed = parse_feed_via_requests(url)  # usa requests para evitar 403
            for entry in feed.entries:
                titulo = entry.get("title", "")
                resumo = entry.get("summary", "") or entry.get("description", "") or ""
                link = entry.get("link", "")
                data = tentar_parse_datetime(entry)

                texto_alvo = f"{titulo} {resumo}"
                if contem_keywords(texto_alvo, keywords):
                    if (data and dentro_da_janela(data, dias)) or (data is None):
                        resultados.append({
                            "fonte": fonte,
                            "titulo": titulo.strip(),
                            "link": link,
                            "resumo": BeautifulSoup(resumo, "html.parser").get_text(" ", strip=True),
                            "data": data.isoformat() if data else "",
                            "tem_data": bool(data),
                            "metodo": "RSS",
                        })
        except Exception:
            logging.warning("Erro ao processar RSS de %s (%s)", fonte, url, exc_info=True)
    return resultados

# ------------------------------
# Coleta via HTML (fallback simples)
# ------------------------------
def coletar_via_html(keywords=KEYWORDS):
    resultados = []
    for fonte, url in HTML_SOURCES.items():
        resp = http_get(url)
        if not resp:
            continue
        try:
            soup = BeautifulSoup(resp.text, "html.parser")
            candidatos = []
            candidatos.extend(soup.select("a[href]"))
            vistos = set()
            for a in candidatos:
                href = a.get("href") or ""
                texto = a.get_text(" ", strip=True) or ""
                alvo = f"{texto} {href}"
                if not href or href.startswith("#"):
                    continue
                if href.startswith("/"):
                    from urllib.parse import urljoin
                    href = urljoin(url, href)
                if contem_keywords(alvo, keywords):
                    chave = (href, texto.lower())
                    if chave in vistos:
                        continue
                    vistos.add(chave)
                    resultados.append({
                        "fonte": fonte,
                        "titulo": texto[:140] if texto else "(sem título)",
                        "link": href,
                        "resumo": "",
                        "data": "",
                        "tem_data": False,
                        "metodo": "HTML",
                    })
        except Exception:
            logging.warning("Erro ao fazer parse HTML de %s", url, exc_info=True)
    return resultados

# ------------------------------
# Consolidação e formatação
# ------------------------------
def deduplicar(registros):
    vistos = set()
    unicos = []
    for r in registros:
        chave = (r["titulo"].strip().lower(), r["link"])
        if chave in vistos:
            continue
        vistos.add(chave)
        unicos.append(r)
    return unicos

def ordenar(registros):
    def key(r):
        dt = None
        try:
            dt = datetime.fromisoformat(r["data"]) if r["data"] else None
        except Exception:
            dt = None
        return (-int(r["tem_data"]), dt if dt else datetime.min.replace(tzinfo=TZ_FORTALEZA), r["fonte"], r["titulo"])
    return sorted(registros, key=key, reverse=True)

def formatar_email(registros, dias=RECENCIA_DIAS):
    if not registros:
        return f"Nenhuma promoção de transferência de milhas encontrada nos últimos {dias} dias.\n"
    linhas = [f"📣 Promoções de transferência (últimos {dias} dias)\n"]
    for r in registros:
        dt_str = ""
        if r["data"]:
            try:
                dt = datetime.fromisoformat(r["data"])
                dt_str = dt.astimezone(TZ_FORTALEZA).strftime("%d/%m/%Y %H:%M")
            except Exception:
                dt_str = r["data"]
        bloco = [
            f"• Fonte: {r['fonte']}",
            f"  Título: {r['titulo']}",
            f"  Link: {r['link']}",
        ]
        if dt_str:
            bloco.append(f"  Data: {dt_str}")
        if r["resumo"]:
            bloco.append(f"  Resumo: {r['resumo']}")
        bloco.append("")
        linhas.append("\n".join(bloco))
    return "\n".join(linhas)

def salvar_log_csv(registros, caminho_csv=ARQ_LOG_CSV):
    os.makedirs(os.path.dirname(caminho_csv), exist_ok=True)
    cabec = ["coleta_em", "fonte", "titulo", "link", "data", "metodo"]
    existe = os.path.exists(caminho_csv)
    existentes = set()
    if existe:
        try:
            with open(caminho_csv, "r", newline="", encoding="utf-8") as f:
                rd = csv.DictReader(f)
                for row in rd:
                    existentes.add((row.get("fonte",""), row.get("link","")))
        except Exception:
            pass
    with open(caminho_csv, "a", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=cabec)
        if not existe:
            wr.writeheader()
        agora_str = AGORA.strftime("%Y-%m-%d %H:%M:%S%z")
        for r in registros:
            chave = (r["fonte"], r["link"])
            if chave in existentes:
                continue
            wr.writerow({
                "coleta_em": agora_str,
                "fonte": r["fonte"],
                "titulo": r["titulo"],
                "link": r["link"],
                "data": r["data"],
                "metodo": r["metodo"],
            })

# ------------------------------
# Coletor principal
# ------------------------------
def buscar_promos_milhas(keywords=KEYWORDS, dias=RECENCIA_DIAS, usar_html_fallback=True):
    rss = coletar_via_rss(keywords, dias)
    html = coletar_via_html(keywords) if usar_html_fallback else []
    combinados = ordenar(deduplicar(rss + html))
    return combinados

# ------------------------------
# Notificação via Telegram (opcional)
# ------------------------------
def enviar_telegram(texto: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": texto[:4000],  # limite básico
            "disable_web_page_preview": True
        }
        r = requests.post(api, data=payload, timeout=15)
        r.raise_for_status()
        print("📨 Telegram enviado.")
    except Exception as e:
        print("Aviso: falha ao enviar Telegram:", e)

# ------------------------------
# Envio de e-mail
# ------------------------------
def enviar_email(corpo_email: str, caminho_credenciais: str = ARQ_CREDENCIAIS, assunto: str | None = None):
    assert os.path.exists(caminho_credenciais), f"Arquivo de credenciais não encontrado: {caminho_credenciais}"
    with open(caminho_credenciais, "r", encoding="utf-8") as f:
        linhas = [ln.strip() for ln in f.read().strip().splitlines() if ln.strip()]
    email_user = linhas[0]
    email_password = linhas[1]  # SENHA DE APP do Gmail
    email_destino = linhas[2]
    if not assunto:
        assunto = f"✈️ Promoções de Transferência de Milhas (últimos {RECENCIA_DIAS} dias)"
    msg = MIMEMultipart()
    msg["From"] = email_user
    msg["To"] = email_destino
    msg["Subject"] = assunto
    msg.attach(MIMEText(corpo_email, "plain", "utf-8"))
    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls(context=ssl.create_default_context())
        server.login(email_user, email_password)
        server.send_message(msg)
        server.quit()
        print("✅ Email enviado com sucesso!")
    except Exception as e:
        print("❌ Erro ao enviar o e-mail:", e)

# ------------------------------
# Execução direta (CLI)
# ------------------------------
def main():
    print(f"🔍 Buscando promoções (Smiles/LATAM Pass + blogs) — janela: {RECENCIA_DIAS} dias...")
    registros = buscar_promos_milhas(KEYWORDS, RECENCIA_DIAS, usar_html_fallback=True)
    corpo = formatar_email(registros, RECENCIA_DIAS)
    print("\n===== PRÉVIA DO EMAIL =====\n")
    print(corpo)
    try:
        salvar_log_csv(registros, ARQ_LOG_CSV)
        print(f"\n📝 Log salvo em: {ARQ_LOG_CSV}")
    except Exception as e:
        print("Aviso: falha ao salvar log CSV:", e)
    # Enviar e-mail (se tiver credenciais)
    if os.path.exists(ARQ_CREDENCIAIS):
        enviar_email(corpo, ARQ_CREDENCIAIS)
    else:
        print(f"\n⚠️ Arquivo de credenciais não encontrado em: {ARQ_CREDENCIAIS}")
        print("Crie um arquivo 'credenciais.txt' com 3 linhas: email, senha de app, destinatário.")
    # Telegram opcional
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        enviar_telegram(corpo)

if __name__ == "__main__":
    main()
