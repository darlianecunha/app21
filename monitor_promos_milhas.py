# -*- coding: utf-8 -*-
import csv, os, re, smtplib, sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urlparse, urljoin

import feedparser, requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser

RECENCIA_DIAS = int(os.getenv("RECENCIA_DIAS", "3"))

POSITIVE_TERMS = [
    r"\b(bônus|bonus)\b",
    r"\b(bonifica(ç|c)ão|bonificado)\b",
    r"\btransfer(ê|e)ncia(s)?\b",
    r"\btransferir\b",
    r"\bpromo(ção|cao)\b",
    r"\blatam pass\b",
    r"\btudo ?azul\b",
    r"\besfera\b",
    r"\blivelo\b",
    r"\bsantander\b",
    r"\bbradesco\b",
    r"\bit(au|a)u\b",
    r"\bbanco do brasil\b",
    r"\bc6 bank\b",
    r"\binter\b",
    r"\bsmiles\b",
]

NEGATIVE_TERMS = []            # ✅ sem bloqueios da Smiles
EXCLUDED_DOMAINS = set()       # ✅ sem bloqueio de domínios

RSS_SOURCES = {
    "Passageiro de Primeira": "https://passageirodeprimeira.com/feed/",
    "Melhores Destinos": "https://www.melhoresdestinos.com.br/feed",
    "Pontos pra Viajar": "https://pontospraviajar.com/feed/",
    "Pontos para Voar": "https://www.pontosparavoar.com/feed/",
    "Meu Milhão de Milhas": "https://meumilhaodemilhas.com/feed/",
    "Passagens Imperdíveis": "https://www.passagensimperdiveis.com.br/feed/",
    # Se quiser somar mais, adicione aqui
}

HTML_SOURCES = {
    # fallback: página oficial LATAM Pass (opcional)
    "LATAM Pass - Promoções": "https://latampass.latam.com/pt_br/promocoes",
}

def limpar_html(txt):
    if not txt: return ""
    try:
        return BeautifulSoup(txt, "html5lib").get_text(" ", strip=True)
    except Exception:
        return re.sub("<[^>]+>", " ", txt)

def dentro_recencia(dt, dias=RECENCIA_DIAS):
    if not dt: return False
    return (datetime.now(timezone.utc) - dt) <= timedelta(days=dias)

def parse_datetime(entry):
    for c in [getattr(entry, "published", None), getattr(entry, "updated", None),
              entry.get("published"), entry.get("updated")]:
        if not c: continue
        try:
            d = dtparser.parse(c)
            if not d.tzinfo: d = d.replace(tzinfo=timezone.utc)
            return d.astimezone(timezone.utc)
        except Exception:
            pass
    return None

def tem_match(padroes, texto):
    for p in padroes:
        if re.search(p, texto, flags=re.IGNORECASE):
            return True
    return False

def dominio_excluido(link):
    host = urlparse(link).netloc.lower()
    return any(host == d or host.endswith("." + d) for d in EXCLUDED_DOMAINS)

def coletar_rss():
    itens = []
    for nome, url in RSS_SOURCES.items():
        try:
            feed = feedparser.parse(url)
            for e in feed.entries:
                title = limpar_html(getattr(e, "title", "") or e.get("title", ""))
                summary = limpar_html(getattr(e, "summary", "") or e.get("summary", ""))
                link = getattr(e, "link", "") or e.get("link", "")
                dt_pub = parse_datetime(e)
                if not title or not link: continue
                if dominio_excluido(link): continue
                if not dentro_recencia(dt_pub): continue
                texto = f"{title} {summary}".lower()
                if tem_match(NEGATIVE_TERMS, texto): continue
                if not tem_match(POSITIVE_TERMS, texto): continue
                itens.append({
                    "fonte": nome,
                    "titulo": title.strip(),
                    "resumo": summary.strip(),
                    "link": link.strip(),
                    "publicado_em": dt_pub.isoformat() if dt_pub else "",
                })
        except Exception as ex:
            print(f"[WARN] RSS falhou {nome}: {ex}")
    return itens

def http_get(url, timeout=20):
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 PromoBot"}, timeout=timeout)
        if r.status_code == 200 and r.text: return r
    except Exception as ex:
        print(f"[WARN] GET falhou {url}: {ex}")
    return None

def coletar_html():
    itens = []
    for nome, url in HTML_SOURCES.items():
        resp = http_get(url)
        if not resp: continue
        try:
            soup = BeautifulSoup(resp.text, "html5lib")
            for a in soup.select("a[href]"):
                href = a.get("href") or ""
                texto = a.get_text(" ", strip=True) or ""
                if not href or href.startswith("#"): continue
                if href.startswith("/"): href = urljoin(url, href)
                if dominio_excluido(href): continue
                alvo = f"{texto} {href}".lower()
                if tem_match(NEGATIVE_TERMS, alvo): continue
                if not tem_match(POSITIVE_TERMS, alvo): continue
                itens.append({
                    "fonte": nome, "titulo": texto[:160] or "(sem título)",
                    "resumo": "", "link": href, "publicado_em": ""
                })
        except Exception as ex:
            print(f"[WARN] HTML falhou {nome}: {ex}")
    return itens

def formatar_email(itens):
    if not itens:
        return f"Nenhuma promoção relevante encontrada nos últimos {RECENCIA_DIAS} dias."
    def key_sort(x): return (x["publicado_em"] or "", x["fonte"], x["titulo"])
    linhas = [f"✈️ Promoções de Transferência (últimos {RECENCIA_DIAS} dias)\n"]
    for i, it in enumerate(sorted(itens, key=key_sort, reverse=True), 1):
        dt_fmt = ""
        if it["publicado_em"]:
            try:
                dt_fmt = dtparser.parse(it["publicado_em"]).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            except Exception:
                dt_fmt = it["publicado_em"]
        linhas.append(f"{i}. [{it['fonte']}] {it['titulo']}\n   Data: {dt_fmt}\n   Link: {it['link']}\n")
    return "\n".join(linhas)

def enviar_email(corpo, assunto=None):
    with open("credenciais.txt", "r", encoding="utf-8") as f:
        email_user, email_pass, email_to = [ln.strip() for ln in f if ln.strip()][:3]
    if not assunto:
        assunto = f"✈️ Promoções de Transferência de Milhas (últimos {RECENCIA_DIAS} dias)"
    msg = MIMEMultipart(); msg["From"]=email_user; msg["To"]=email_to; msg["Subject"]=assunto
    msg.attach(MIMEText(corpo, "plain", "utf-8"))
    s = smtplib.SMTP("smtp.gmail.com", 587); s.starttls(); s.login(email_user, email_pass); s.send_message(msg); s.quit()

def salvar_csv(itens, caminho="monitor_promos_milhas_log.csv"):
    campos = ["timestamp_execucao_utc", "fonte", "titulo", "link", "publicado_em"]
    ts = datetime.now(timezone.utc).isoformat()
    novo = not os.path.exists(caminho)
    with open(caminho, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=campos)
        if novo: w.writeheader()
        for it in itens:
            w.writerow({"timestamp_execucao_utc": ts, **{k: it[k] for k in it if k in campos}})

def main():
    print(f"🔍 Buscando promoções (incluindo Smiles), últimos {RECENCIA_DIAS} dias…")
    itens = coletar_rss() + coletar_html()
    corpo = formatar_email(itens)
    print("\n===== PRÉVIA DO EMAIL =====\n"); print(corpo); print("\n===========================\n")
    try:
        enviar_email(corpo); print("✅ Email enviado com sucesso!")
    except Exception as ex:
        print("❌ Erro ao enviar e-mail:", ex, file=sys.stderr)
    try:
        salvar_csv(itens)
    except Exception as ex:
        print("[WARN] Falha ao salvar CSV:", ex, file=sys.stderr)

if __name__ == "__main__":
    main()
