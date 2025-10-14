# monitor_promos_milhas.py
# Coleta promoções de bônus de transferência de pontos/milhas (SEM SMILES),
# filtra pelos últimos N dias e envia e-mail (e opcionalmente Telegram).
#
# Lê credenciais do arquivo 'credenciais.txt' (3 linhas):
#   1) email_user (Gmail)
#   2) email_app_password (senha de app do Gmail)
#   3) email_destino
#
# Requer (requirements.txt):
#   feedparser
#   beautifulsoup4
#   html5lib
#   requests
#   python-dateutil

import csv
import os
import re
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser

# ===========================
# Configurações principais
# ===========================
RECENCIA_DIAS = int(os.getenv("RECENCIA_DIAS", "3"))

# Termos positivos (o post precisa bater pelo menos um)
POSITIVE_TERMS = [
    r"\b(bônus|bonus)\b",
    r"\b(bonifica(ç|c)ão|bonificado)\b",
    r"\btransfer(ê|e)ncia(s)?\b",
    r"\btransferir\b",
    r"\blatam pass\b",
    r"\btudo ?azul\b",
    r"\besfera\b",
    r"\blivelo\b",
    r"\binter loop\b",
    r"\bbanrisul cartão\b",
    r"\bsantander\b",
    r"\bbradesco\b",
    r"\bitau\b",
    r"\bbanco do brasil\b",
    r"\bc6 bank\b",
    r"\bportabilidade de pontos\b",
]

# Termos negativos (se bater, descarta)
NEGATIVE_TERMS = [
    r"\bsmiles\b",              # remove todo conteúdo relativo à Smiles
    r"\bgol smiles\b",
    r"\bclube smiles\b",
]

# Domínios excluídos (nunca considerar)
EXCLUDED_DOMAINS = {
    "smiles.com.br",
    "blog.smiles.com.br",
    "loja.smiles.com.br",
}

# Fontes RSS — sem nenhuma URL da Smiles
RSS_SOURCES = {
    # Blogs de viagens/finanças com boa cobertura de bônus de transferência
    "Passageiro de Primeira": "https://passageirodeprimeira.com/feed/",
    "Melhores Destinos": "https://www.melhoresdestinos.com.br/feed",
    "Pontos pra Viajar": "https://pontospraviajar.com/feed/",
    "Meu Milhão de Milhas": "https://meumilhaodemilhas.com/feed/",
    "Passagens Imperdíveis": "https://www.passagensimperdiveis.com.br/feed/",
    # Adapte/adicione seus preferidos aqui
}

# ===========================
# Utilidades
# ===========================
def limpar_html(texto):
    if not texto:
        return ""
    try:
        soup = BeautifulSoup(texto, "html5lib")
        return soup.get_text(" ", strip=True)
    except Exception:
        return re.sub("<[^>]+>", " ", texto)

def dentro_recencia(dt, dias=RECENCIA_DIAS):
    if not dt:
        return False
    agora = datetime.now(timezone.utc)
    return (agora - dt) <= timedelta(days=dias)

def parse_datetime(entry):
    # tenta published, updated, etc.
    candidatos = [
        getattr(entry, "published", None),
        getattr(entry, "updated", None),
        entry.get("published"),
        entry.get("updated"),
    ]
    for c in candidatos:
        if not c:
            continue
        try:
            d = dtparser.parse(c)
            # garantir timezone
            if not d.tzinfo:
                d = d.replace(tzinfo=timezone.utc)
            return d.astimezone(timezone.utc)
        except Exception:
            continue
    # fallback: None
    return None

def tem_match(padroes, texto_lower):
    for p in padroes:
        if re.search(p, texto_lower, flags=re.IGNORECASE):
            return True
    return False

def dominio_excluido(link):
    try:
        host = urlparse(link).netloc.lower()
        return any(host == d or host.endswith("." + d) for d in EXCLUDED_DOMAINS)
    except Exception:
        return False

# ===========================
# Coleta
# ===========================
def coletar_itens():
    itens = []
    for nome, url in RSS_SOURCES.items():
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                title = limpar_html(getattr(entry, "title", "") or entry.get("title", ""))
                summary = limpar_html(getattr(entry, "summary", "") or entry.get("summary", ""))
                link = getattr(entry, "link", "") or entry.get("link", "")
                dt_pub = parse_datetime(entry)

                # filtros básicos
                if not title or not link:
                    continue
                if dominio_excluido(link):
                    continue
                if not dentro_recencia(dt_pub, RECENCIA_DIAS):
                    continue

                texto = f"{title} {summary}".lower()

                # Filtro negativo primeiro
                if tem_match(NEGATIVE_TERMS, texto):
                    continue

                # Filtro positivo (precisa bater algo de bônus/transfer etc.)
                if not tem_match(POSITIVE_TERMS, texto):
                    continue

                itens.append({
                    "fonte": nome,
                    "titulo": title.strip(),
                    "resumo": summary.strip(),
                    "link": link.strip(),
                    "publicado_em": dt_pub.isoformat() if dt_pub else "",
                })
        except Exception as e:
            print(f"[WARN] Falha ao ler {nome}: {e}")
    return itens

# ===========================
# Formatação do e-mail
# ===========================
def formatar_email(itens):
    if not itens:
        return "Nenhuma promoção relevante (sem Smiles) encontrada nos últimos %d dias." % RECENCIA_DIAS

    linhas = [f"✈️ Promoções de Transferência (últimos {RECENCIA_DIAS} dias) — SEM Smiles\n"]
    for i, item in enumerate(sorted(itens, key=lambda x: x["publicado_em"], reverse=True), 1):
        dt_fmt = ""
        if item["publicado_em"]:
            try:
                dt_fmt = dtparser.parse(item["publicado_em"]).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            except Exception:
                dt_fmt = item["publicado_em"]
        linhas.append(
            f"{i}. [{item['fonte']}] {item['titulo']}\n"
            f"   Data: {dt_fmt}\n"
            f"   Link: {item['link']}\n"
        )
    return "\n".join(linhas)

# ===========================
# E-mail + Telegram
# ===========================
def enviar_email(corpo_email, assunto="✈️ Promoções de Transferência de Milhas (últimos 3 dias) • SEM Smiles"):
    # Lê credenciais.txt (3 linhas)
    caminho = "credenciais.txt"
    assert os.path.exists(caminho), f"Arquivo de credenciais não encontrado: {caminho}"
    with open(caminho, "r", encoding="utf-8") as f:
        linhas = [ln.strip() for ln in f.read().splitlines() if ln.strip()]
    email_user, email_pass, email_to = linhas[0], linhas[1], linhas[2]

    msg = MIMEMultipart()
    msg["From"] = email_user
    msg["To"] = email_to
    msg["Subject"] = assunto
    msg.attach(MIMEText(corpo_email, "plain", "utf-8"))

    server = smtplib.SMTP("smtp.gmail.com", 587)
    server.starttls()
    server.login(email_user, email_pass)
    server.send_message(msg)
    server.quit()

def enviar_telegram(texto):
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data={"chat_id": chat_id, "text": texto},
            timeout=20,
        )
        print("📨 Telegram enviado.")
    except Exception as e:
        print(f"[WARN] Telegram falhou: {e}")

# ===========================
# Persistência do log
# ===========================
def salvar_csv(itens, caminho="monitor_promos_milhas_log.csv"):
    campos = ["timestamp_execucao_utc", "fonte", "titulo", "link", "publicado_em"]
    ts = datetime.now(timezone.utc).isoformat()
    novo_arquivo = not os.path.exists(caminho)

    with open(caminho, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=campos)
        if novo_arquivo:
            w.writeheader()
        for it in itens:
            w.writerow({
                "timestamp_execucao_utc": ts,
                "fonte": it["fonte"],
                "titulo": it["titulo"],
                "link": it["link"],
                "publicado_em": it["publicado_em"],
            })
    print(f"📝 Log salvo em: {caminho}")

# ===========================
# Execução principal
# ===========================
def main():
    print(f"🔍 Buscando promoções (sem Smiles), últimos {RECENCIA_DIAS} dias…")
    itens = coletar_itens()

    corpo = formatar_email(itens)
    print("\n===== PRÉVIA DO EMAIL =====\n")
    print(corpo)
    print("\n===========================\n")

    try:
        enviar_email(corpo)
        print("✅ Email enviado com sucesso!")
    except Exception as e:
        print("❌ Erro ao enviar e-mail:", e, file=sys.stderr)

    # mandar resumo curto no Telegram (se configurado)
    resumo_tg = "Sem novidades relevantes (sem Smiles) nos últimos %d dias." % RECENCIA_DIAS
    if itens:
        top = itens[0]
        resumo_tg = f"Top achado (sem Smiles): [{top['fonte']}] {top['titulo']} — {top['link']}"
    enviar_telegram(resumo_tg)

    # salvar CSV
    try:
        salvar_csv(itens)
    except Exception as e:
        print("[WARN] Falha ao salvar CSV:", e, file=sys.stderr)

if __name__ == "__main__":
    main()

        
