#!/usr/bin/env python3
"""
Monitor de passagens aéreas -> avisos no iPhone via Telegram.

Como funciona, todo dia:
  1. VARREDURA (Travelpayouts / Aviasales, grátis): pega os preços mais
     baratos já encontrados para cada mês, em qualquer data com a duração
     configurada (ex.: 11 a 16 dias).
  2. CONFERÊNCIA (Google Flights via SerpApi): confere as datas mais
     baratas da varredura com os passageiros reais e o preço total atual.
  3. AVISO: manda Telegram quando o preço conferido é o menor já visto,
     está "baixo" segundo o Google ou abaixo da sua meta.

Sem token da Travelpayouts, o robô sorteia datas para conferir.

Uso:
  python monitor.py                     execução normal (precisa das chaves)
  python monitor.py --simular           testa tudo com preços inventados
  python monitor.py --varredura         só testa a varredura (não gasta SerpApi)
  python monitor.py --listar            mostra o período e as combinações
  python monitor.py --testar-telegram   manda uma mensagem de teste
  python monitor.py --bot               lê e responde suas mensagens no Telegram
  python monitor.py --comando "texto"   testa um comando do bot sem Telegram

Variáveis de ambiente:
  SERPAPI_KEY, TRAVELPAYOUTS_TOKEN, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

Não precisa instalar nada além do Python 3.11+.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import random
import re
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
ARQ_CONFIG = RAIZ / "config.toml"
ARQ_HIST = RAIZ / "data" / "historico.json"
ARQ_HIST_SIM = RAIZ / "data" / "historico_simulado.json"
ARQ_RELATORIO = RAIZ / "RELATORIO.md"
ARQ_VIAGENS = RAIZ / "data" / "viagens.json"
ARQ_BOT = RAIZ / "data" / "bot.json"
FUSO_BR = dt.timezone(dt.timedelta(hours=-3))
MAX_OBS_POR_VIAGEM = 800
MESES_PT = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]


# --------------------------------------------------------------------- util
def hoje() -> dt.date:
    return dt.datetime.now(FUSO_BR).date()


def agora_iso() -> str:
    return dt.datetime.now(FUSO_BR).isoformat(timespec="minutes")


def brl(valor: float) -> str:
    return "R$ " + f"{valor:,.0f}".replace(",", ".")


def data_br(d: str | dt.date) -> str:
    if isinstance(d, str):
        d = dt.date.fromisoformat(d[:10])
    return d.strftime("%d/%m/%Y")


def mes_br(chave: str) -> str:
    ano, mes = chave.split("-")
    return f"{MESES_PT[int(mes) - 1]}/{ano}"


def dias_entre(ida: str, volta: str) -> int:
    return (dt.date.fromisoformat(volta[:10]) - dt.date.fromisoformat(ida[:10])).days


def duracao_txt(minutos) -> str:
    if not minutos:
        return "?"
    return f"{int(minutos) // 60}h{int(minutos) % 60:02d}"


def descricao_pax(p: dict) -> str:
    partes = []
    if p.get("adultos"):
        partes.append(f"{p['adultos']} adulto{'s' if p['adultos'] > 1 else ''}")
    if p.get("criancas"):
        partes.append(f"{p['criancas']} criança{'s' if p['criancas'] > 1 else ''}")
    bebes = p.get("bebes_colo", 0) + p.get("bebes_assento", 0)
    if bebes:
        partes.append(f"{bebes} bebê{'s' if bebes > 1 else ''}")
    return " + ".join(partes)


def pagantes(p: dict) -> int:
    return p.get("adultos", 1) + p.get("criancas", 0) + p.get("bebes_assento", 0)


def link_relatorio() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    return f"https://github.com/{repo}/blob/main/RELATORIO.md" if repo else "RELATORIO.md"


def get_json(url: str, headers: dict | None = None, timeout: int = 90) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "monitor-passagens/2.0", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        corpo = e.read().decode("utf-8", "replace")
        try:
            msg = json.loads(corpo).get("error", corpo)
        except (json.JSONDecodeError, AttributeError):
            msg = corpo
        raise RuntimeError(f"HTTP {e.code}: {str(msg)[:300]}") from None


def carregar_config() -> dict:
    with open(ARQ_CONFIG, "rb") as f:
        return tomllib.load(f)


def carregar_viagens(cfg: dict) -> list[dict]:
    """Viagens ficam em data/viagens.json (o bot do Telegram edita). Sem o arquivo, usa o config.toml."""
    if ARQ_VIAGENS.exists():
        with open(ARQ_VIAGENS, encoding="utf-8") as f:
            return json.load(f)
    return [dict(v) for v in cfg.get("viagens", [])]


def salvar_viagens(viagens: list[dict]) -> None:
    ARQ_VIAGENS.parent.mkdir(parents=True, exist_ok=True)
    with open(ARQ_VIAGENS, "w", encoding="utf-8") as f:
        json.dump(viagens, f, ensure_ascii=False, indent=1)


def carregar_hist(caminho: Path) -> dict:
    if caminho.exists():
        with open(caminho, encoding="utf-8") as f:
            hist = json.load(f)
    else:
        hist = {}
    hist.setdefault("versao", 2)
    hist.setdefault("rodada", 0)
    hist.setdefault("viagens", {})
    hist.setdefault("varredura", {})
    hist.pop("cursores", None)  # usado só na versão 1
    return hist


def migrar_historico(hist: dict, viagens: list[dict]) -> None:
    """Junta o histórico das viagens antigas (por ano/mês fixo) na viagem nova."""
    for v in viagens:
        for antigo in v.get("historico_antigo", []):
            est_antigo = hist["viagens"].pop(antigo, None)
            if not est_antigo:
                continue
            est = hist["viagens"].setdefault(v["id"], {"observacoes": []})
            est["observacoes"] = sorted(est["observacoes"] + est_antigo.get("observacoes", []), key=lambda o: o["em"])
            for o in est["observacoes"]:
                if not est.get("recorde") or o["preco"] < est["recorde"]["preco"]:
                    est["recorde"] = o
            if est_antigo.get("ultimo_alerta") and not est.get("ultimo_alerta"):
                est["ultimo_alerta"] = est_antigo["ultimo_alerta"]


def salvar_hist(caminho: Path, hist: dict) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with open(caminho, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=1)


# ------------------------------------------------------------ datas válidas
def periodo(cfg: dict, ref: dt.date) -> tuple[dt.date, dt.date]:
    """Primeira ida possível e última volta possível."""
    return ref + dt.timedelta(days=1), ref + dt.timedelta(days=cfg["geral"].get("dias_max_antecedencia", 300))


def janela(viagem: dict, cfg: dict, ref: dt.date) -> tuple[dt.date, dt.date]:
    """Período da viagem: o geral, cortado pelas datas "de" e "ate" da viagem (se houver)."""
    inicio, fim = periodo(cfg, ref)
    if viagem.get("de"):
        inicio = max(inicio, dt.date.fromisoformat(viagem["de"]))
    if viagem.get("ate"):
        fim = min(fim, dt.date.fromisoformat(viagem["ate"]))
    return inicio, fim


def data_valida(viagem: dict, cfg: dict, ref: dt.date, ida: dt.date, volta: dt.date) -> bool:
    inicio, fim = janela(viagem, cfg, ref)
    dur = (volta - ida).days
    if not (viagem["dias_min"] <= dur <= viagem["dias_max"]):
        return False
    if ida < inicio or volta > fim:
        return False
    meses = viagem.get("meses")
    return not meses or ida.month in meses


def todas_combinacoes(viagem: dict, cfg: dict, ref: dt.date) -> list[tuple[dt.date, dt.date]]:
    inicio, fim = janela(viagem, cfg, ref)
    pares = []
    ida = inicio
    while ida + dt.timedelta(days=viagem["dias_min"]) <= fim:
        for dur in range(viagem["dias_min"], viagem["dias_max"] + 1):
            volta = ida + dt.timedelta(days=dur)
            if data_valida(viagem, cfg, ref, ida, volta):
                pares.append((ida, volta))
        ida += dt.timedelta(days=1)
    return pares


def meses_do_periodo(viagem: dict, cfg: dict, ref: dt.date) -> list[str]:
    inicio, fim = janela(viagem, cfg, ref)
    meses, d = [], inicio.replace(day=1)
    while d <= fim:
        if not viagem.get("meses") or d.month in viagem["meses"]:
            meses.append(d.strftime("%Y-%m"))
        d = (d + dt.timedelta(days=32)).replace(day=1)
    return meses


def dias_txt(v: dict) -> str:
    return f"{v['dias_min']} dias" if v["dias_min"] == v["dias_max"] else f"{v['dias_min']} a {v['dias_max']} dias"


def periodo_txt(v: dict) -> str:
    partes = []
    if v.get("de") and v.get("ate") and dias_entre(v["de"], v["ate"]) == v["dias_min"] == v["dias_max"]:
        return f"ida {data_br(v['de'])}, volta {data_br(v['ate'])}"
    if v.get("de"):
        partes.append(f"ida a partir de {data_br(v['de'])}")
    if v.get("ate"):
        partes.append(f"volta até {data_br(v['ate'])}")
    if v.get("meses"):
        partes.append("ida em " + ", ".join(MESES_PT[m - 1] for m in v["meses"]))
    return ", ".join(partes) or "qualquer data"


# ------------------------------------------------- 1. varredura Travelpayouts
def varrer_travelpayouts(viagem: dict, cfg: dict, ref: dt.date, token: str) -> list[dict]:
    """Preços em cache da Aviasales (1 adulto). Retorna candidatos ordenados por preço."""
    token = token.strip()
    melhores: dict[tuple[str, str], dict] = {}
    for mes in meses_do_periodo(viagem, cfg, ref):
        params = {
            "origin": viagem["origem"],
            "destination": viagem["destino"],
            "departure_at": mes,
            "one_way": "false",
            "direct": "false",
            "currency": "brl",
            "market": "br",
            "sorting": "price",
            "unique": "false",
            "limit": 1000,
            "token": token,
        }
        dados = get_json("https://api.travelpayouts.com/aviasales/v3/prices_for_dates?" + urllib.parse.urlencode(params),
                         headers={"X-Access-Token": token, "Accept-Encoding": "identity"})
        if dados.get("success") is False:
            raise RuntimeError(f"Travelpayouts: {dados.get('error') or dados}")
        for item in dados.get("data") or []:
            try:
                ida = dt.date.fromisoformat(str(item["departure_at"])[:10])
                volta = dt.date.fromisoformat(str(item["return_at"])[:10])
                preco = float(item["price"])
            except (KeyError, TypeError, ValueError):
                continue
            if not data_valida(viagem, cfg, ref, ida, volta):
                continue
            chave = (ida.isoformat(), volta.isoformat())
            if chave not in melhores or preco < melhores[chave]["preco_pp"]:
                melhores[chave] = {
                    "ida": chave[0],
                    "volta": chave[1],
                    "preco_pp": preco,
                    "companhia": item.get("airline") or "?",
                    "escalas": item.get("transfers"),
                }
    return sorted(melhores.values(), key=lambda c: c["preco_pp"])


def varrer_simulado(viagem: dict, cfg: dict, ref: dt.date) -> list[dict]:
    base = 3600 if viagem["destino"] == "MCO" else 1900
    pares = todas_combinacoes(viagem, cfg, ref)
    amostra = random.sample(pares, min(len(pares), 150))
    cands = [{
        "ida": ida.isoformat(), "volta": volta.isoformat(),
        "preco_pp": round(max(600, random.gauss(base, base * 0.2))),
        "companhia": random.choice(["LA", "G3", "AD", "AA", "CM", "AR"]),
        "escalas": random.choice([0, 1, 1, 2]),
    } for ida, volta in amostra]
    return sorted(cands, key=lambda c: c["preco_pp"])


def resumo_varredura(cands: list[dict]) -> dict:
    por_mes: dict[str, dict] = {}
    for c in cands:
        m = c["ida"][:7]
        if m not in por_mes or c["preco_pp"] < por_mes[m]["preco_pp"]:
            por_mes[m] = c
    return {"em": agora_iso(), "candidatos": len(cands), "por_mes": dict(sorted(por_mes.items()))}


# ------------------------------------------------ 2. conferência no Google
STOPS = {None: 0, 0: 1, 1: 2, 2: 3}  # parâmetro "stops" da SerpApi


def link_google(viagem: dict, ida: str, volta: str) -> str:
    q = f"Flights from {viagem['origem']} to {viagem['destino']} on {ida} through {volta}"
    return "https://www.google.com/travel/flights?hl=pt-BR&curr=BRL&q=" + urllib.parse.quote(q)


def buscar_serpapi(viagem: dict, ida: str, volta: str, chave: str) -> dict | None:
    pax = viagem.get("passageiros", {})
    params = {
        "engine": "google_flights",
        "departure_id": viagem["origem"],
        "arrival_id": viagem["destino"],
        "outbound_date": ida,
        "return_date": volta,
        "type": "1",  # ida e volta
        "currency": "BRL",
        "gl": "br",
        "hl": "en",
        "adults": pax.get("adultos", 1),
        "children": pax.get("criancas", 0),
        "infants_on_lap": pax.get("bebes_colo", 0),
        "infants_in_seat": pax.get("bebes_assento", 0),
        "stops": STOPS.get(viagem.get("escalas_max"), 0),
        "api_key": chave,
    }
    try:
        dados = get_json("https://serpapi.com/search.json?" + urllib.parse.urlencode(params))
    except RuntimeError as e:
        raise RuntimeError(f"SerpApi {e}") from None

    if dados.get("error"):
        # "no results" não é erro do robô, só não há voo nessa data
        if "hasn't returned any results" in dados["error"]:
            return None
        raise RuntimeError(f"SerpApi: {dados['error']}")

    voos = (dados.get("best_flights") or []) + (dados.get("other_flights") or [])
    voos = [v for v in voos if isinstance(v.get("price"), (int, float))]
    if not voos:
        return None
    melhor = min(voos, key=lambda v: v["price"])
    trechos = melhor.get("flights") or []
    companhias = sorted({t.get("airline") for t in trechos if t.get("airline")})
    insights = dados.get("price_insights") or {}
    link = (dados.get("search_metadata") or {}).get("google_flights_url") or link_google(viagem, ida, volta)
    return {
        "preco": float(melhor["price"]),
        "companhias": ", ".join(companhias) or "?",
        "escalas": max(len(trechos) - 1, 0),
        "duracao_min": melhor.get("total_duration"),
        "nivel_google": insights.get("price_level"),
        "faixa_tipica": insights.get("typical_price_range"),
        "link": link,
    }


def buscar_simulado(viagem: dict, ida: str, volta: str) -> dict:
    pax = viagem.get("passageiros", {})
    base = 4200 if viagem["destino"] == "MCO" else 2400
    preco = max(800, random.gauss(base, base * 0.18)) * pagantes(pax) + pax.get("bebes_colo", 0) * base * 0.1
    tipico = [round(base * 0.85 * pagantes(pax)), round(base * 1.2 * pagantes(pax))]
    nivel = "low" if preco < tipico[0] else ("high" if preco > tipico[1] else "typical")
    return {
        "preco": round(preco),
        "companhias": random.choice(["LATAM", "GOL", "Azul", "American", "Copa", "Aerolíneas Argentinas"]),
        "escalas": random.choice([0, 1, 1, 2]),
        "duracao_min": random.randint(240, 1400),
        "nivel_google": nivel,
        "faixa_tipica": tipico,
        "link": link_google(viagem, ida, volta),
    }


def escolher_para_conferir(viagem: dict, cands: list[dict], est: dict, cfg: dict, ref: dt.date,
                           cota: int, ja: set) -> list[tuple[str, str, str]]:
    """Datas a conferir no Google: as mais baratas da varredura e, se faltar, sorteadas."""
    dias_reconf = cfg["geral"].get("reconferir_apos_dias", 3)
    corte = (ref - dt.timedelta(days=dias_reconf)).isoformat()
    recentes = {(o["ida"], o["volta"]) for o in est.get("observacoes", []) if o["em"][:10] > corte}
    escolhidas = []
    for c in cands:
        if len(escolhidas) >= cota:
            break
        par = (c["ida"], c["volta"])
        if par in recentes or par in ja:
            continue
        escolhidas.append((*par, "varredura"))
        ja.add(par)
    if len(escolhidas) < cota:
        pares = [(i.isoformat(), v.isoformat()) for i, v in todas_combinacoes(viagem, cfg, ref)]
        livres = [p for p in pares if p not in recentes and p not in ja]
        for par in random.sample(livres, min(len(livres), cota - len(escolhidas))):
            escolhidas.append((*par, "sorteio"))
            ja.add(par)
    return escolhidas


# ----------------------------------------------------------------- telegram
def telegram(texto: str, enviar: bool) -> None:
    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    print("\n--- mensagem " + ("(Telegram) " if enviar and token else "(só na tela) ") + "---")
    print(texto)
    if not (enviar and token and chat):
        return
    dados = urllib.parse.urlencode({
        "chat_id": chat,
        "text": texto,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=dados)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
    except urllib.error.HTTPError as e:
        print(f"!! Falha ao enviar no Telegram: HTTP {e.code} {e.read().decode('utf-8', 'replace')[:300]}")


# --------------------------------------------------------------- alertas
def avaliar(estado: dict, obs: dict, viagem: dict, cfg: dict, ref: dt.date) -> list[str]:
    motivos = []
    recorde = estado.get("recorde")
    n_antes = len(estado.get("observacoes", []))
    minimo_obs = cfg["geral"].get("observacoes_antes_de_alertar", 10)

    if recorde and obs["preco"] < recorde["preco"] and n_antes >= minimo_obs:
        motivos.append(
            f"🏆 <b>NOVO MENOR PREÇO HISTÓRICO</b> desde que comecei a monitorar "
            f"(antes: {brl(recorde['preco'])} em {data_br(recorde['ida'])})"
        )
    alvo = viagem.get("preco_alvo")
    if alvo and obs["preco"] <= alvo:
        motivos.append(f"🎯 Abaixo da sua meta de {brl(alvo)}")
    faixa = obs.get("faixa_tipica")
    if obs.get("nivel_google") == "low":
        txt = "📉 O Google Flights considera este preço <b>BAIXO</b> para a rota"
        if faixa:
            txt += f" (o normal é {brl(faixa[0])} a {brl(faixa[1])})"
        motivos.append(txt)

    if not motivos:
        return []
    # evita repetir o mesmo aviso: só avisa se for mais barato que o último
    # aviso, ou se o último aviso já tem 7 dias
    ultimo = estado.get("ultimo_alerta")
    if ultimo:
        dias = (ref - dt.date.fromisoformat(ultimo["em"][:10])).days
        if obs["preco"] >= ultimo["preco"] and dias < 7:
            return []
    return motivos


def msg_alerta(viagem: dict, obs: dict, motivos: list[str]) -> str:
    escalas = "voo direto" if obs["escalas"] == 0 else f"{obs['escalas']} escala{'s' if obs['escalas'] > 1 else ''}"
    return (
        f"✈️ <b>{html.escape(viagem['nome'])}</b>: {brl(obs['preco'])}\n"
        f"👨‍👩‍👦 total para {descricao_pax(viagem.get('passageiros', {}))}\n"
        f"📅 {data_br(obs['ida'])} → {data_br(obs['volta'])} ({dias_entre(obs['ida'], obs['volta'])} dias)\n"
        f"🛫 {html.escape(obs['companhias'])} · {escalas} · ida {duracao_txt(obs['duracao_min'])}\n\n"
        + "\n".join(motivos)
        + f"\n\n<a href=\"{html.escape(obs['link'])}\">👉 Ver e comprar no Google Flights</a>\n"
        "<i>Preços mudam rápido. Confira o valor final antes de pagar.</i>"
    )


def msg_resumo(viagens: list[dict], hist: dict, ref: dt.date) -> str:
    linhas = ["📊 <b>Resumo semanal das passagens</b>", "O robô está funcionando ✅ Toque no preço para abrir a passagem.",
              f"<a href=\"{html.escape(link_relatorio())}\">📄 Ver relatório completo</a>", ""]
    corte = (ref - dt.timedelta(days=7)).isoformat()
    for v in viagens:
        est = hist["viagens"].get(v["id"], {})
        linhas.append(f"<b>{html.escape(v['nome'])}</b> ({dias_txt(v)}, {periodo_txt(v)})")
        rec = est.get("recorde")
        if not rec:
            linhas.append("   Ainda sem preços conferidos.")
        else:
            semana = [o for o in est.get("observacoes", []) if o["em"] >= corte]
            linhas.append(f"   Menor já visto: <a href=\"{html.escape(rec['link'])}\">{brl(rec['preco'])}</a> "
                          f"({data_br(rec['ida'])} → {data_br(rec['volta'])})")
            if semana:
                m = min(semana, key=lambda o: o["preco"])
                linhas.append(f"   Melhor desta semana: <a href=\"{html.escape(m['link'])}\">{brl(m['preco'])}</a> "
                              f"({data_br(m['ida'])} → {data_br(m['volta'])})")
            linhas.append(f"   Buscas no Google: {len(est.get('observacoes', []))}")
        var = hist["varredura"].get(v["id"])
        if var and var.get("por_mes"):
            mes, c = min(var["por_mes"].items(), key=lambda kv: kv[1]["preco_pp"])
            linhas.append(f"   Mês mais barato na varredura: {mes_br(mes)} (~{brl(c['preco_pp'])} por pessoa)")
        linhas.append("")
    return "\n".join(linhas).strip()


# ---------------------------------------------------------------- relatório
def gerar_relatorio(viagens: list[dict], hist: dict, cfg: dict, ref: dt.date, caminho: Path) -> None:
    inicio, fim = periodo(cfg, ref)
    out = ["# ✈️ Relatório de passagens", "",
           f"_Atualizado em {agora_iso().replace('T', ' ')} (horário de Brasília)_  ",
           f"_Período pesquisado: ida a partir de {data_br(inicio)}, volta até {data_br(fim)}_", ""]
    corte = (ref - dt.timedelta(days=14)).isoformat()
    for v in viagens:
        est = hist["viagens"].get(v["id"], {})
        obs = est.get("observacoes", [])
        out += [f"## {v['nome']}", "",
                f"{v['origem']} → {v['destino']} · {dias_txt(v)} · {periodo_txt(v)} · "
                f"{descricao_pax(v.get('passageiros', {}))}", ""]
        rec = est.get("recorde")
        if rec:
            out += [f"**Menor preço já visto: {brl(rec['preco'])}** ({data_br(rec['ida'])} → {data_br(rec['volta'])}, "
                    f"{rec['companhias']}, visto em {rec['em'][:10]}) · [abrir]({rec['link']})", ""]
        # último preço conferido de cada par de datas, só dos últimos 14 dias
        atual = {}
        for o in obs:
            if o["em"] >= corte:
                atual[(o["ida"], o["volta"])] = o
        if atual:
            out += ["### Mais baratos conferidos no Google (últimos 14 dias)", "",
                    "| Ida | Volta | Dias | Total | Companhia | Escalas | Google | Visto em |",
                    "|---|---|---|---|---|---|---|---|"]
            for o in sorted(atual.values(), key=lambda o: o["preco"])[:15]:
                nivel = {"low": "🟢 baixo", "typical": "🟡 normal", "high": "🔴 alto"}.get(o.get("nivel_google"), "-")
                out.append(f"| {data_br(o['ida'])} | {data_br(o['volta'])} | {dias_entre(o['ida'], o['volta'])} | "
                           f"[{brl(o['preco'])}]({o['link']}) | {o['companhias']} | {o['escalas']} | {nivel} | {o['em'][:10]} |")
            out.append("")
        var = hist["varredura"].get(v["id"])
        if var and var.get("por_mes"):
            out += [f"### Varredura Aviasales: menor preço por pessoa em cada mês de ida",
                    f"_{var['candidatos']} combinações encontradas em {var['em'][:10]}. "
                    f"Estimativa para 1 adulto; o total real vem da conferência no Google._", "",
                    "| Mês | Por pessoa | Total estimado | Ida → Volta |", "|---|---|---|---|"]
            n = pagantes(v.get("passageiros", {}))
            for mes, c in var["por_mes"].items():
                out.append(f"| {mes_br(mes)} | {brl(c['preco_pp'])} | ~{brl(c['preco_pp'] * n)} | "
                           f"{data_br(c['ida'])} → {data_br(c['volta'])} |")
            out.append("")
        if not rec and not var:
            out += ["_Ainda sem dados._", ""]
    caminho.write_text("\n".join(out), encoding="utf-8")


# --------------------------------------------------------- bot do Telegram
MESES_NOMES = {
    "janeiro": 1, "jan": 1, "fevereiro": 2, "fev": 2, "marco": 3, "mar": 3, "abril": 4, "abr": 4,
    "maio": 5, "mai": 5, "junho": 6, "jun": 6, "julho": 7, "jul": 7, "agosto": 8, "ago": 8,
    "setembro": 9, "set": 9, "outubro": 10, "out": 10, "novembro": 11, "nov": 11, "dezembro": 12, "dez": 12,
}
PALAVRAS_SOLTAS = {"de", "da", "do", "a", "ate", "e", "entre", "dias", "dia", "para", "pra", "em", "no", "na",
                   "por", "com", "volta", "ida", "periodo", "-"}

AJUDA = (
    "✈️ <b>Comandos do monitor de passagens</b>\n\n"
    "<b>Nova viagem</b> (destino + dias):\n"
    "<code>/nova Orlando 11-16</code>  qualquer data, 11 a 16 dias\n"
    "<code>/nova Lisboa 10</code>  qualquer data, 10 dias\n"
    "<code>/nova Bariloche 12 em setembro</code>  ida em setembro\n"
    "<code>/nova Paris 10-14 de 01/03/2027 a 30/04/2027</code>  dentro desse período\n"
    "<code>/nova Miami 10/03/2027 a 25/03/2027</code>  datas exatas\n"
    "Atalho: <code>/ushuaia 7</code> ou <code>Lisboa 10</code> também cria a viagem.\n\n"
    "<b>Ver e mudar</b> (o número vem de /viagens):\n"
    "/viagens  lista as viagens\n"
    "<code>/dias 1 12-15</code>  muda a duração\n"
    "<code>/periodo 1 01/09/2027 a 30/09/2027</code>  muda o período\n"
    "<code>/periodo 1 setembro outubro</code>  só esses meses\n"
    "<code>/periodo 1 livre</code>  qualquer data de novo\n"
    "<code>/pessoas 1 2 1</code>  2 adultos e 1 criança\n"
    "<code>/meta 1 15000</code>  avisa abaixo de R$ 15.000 (0 tira)\n"
    "<code>/pausar 1</code>, <code>/ativar 1</code>, <code>/remover 1</code>\n"
    "/precos  resumo dos preços\n\n"
    "<i>Respondo em até ~15 min. A busca no Google roda todo dia às 8h e divide as buscas entre as viagens ativas.</i>"
)


COMANDOS = {"start", "ajuda", "help", "menu", "comandos", "viagens", "lista", "listar", "precos", "resumo",
            "nova", "novo", "viagem", "adicionar", "remover", "apagar", "excluir", "pausar", "ativar",
            "dias", "periodo", "pessoas", "meta"}


def sem_acento(t: str) -> str:
    return t.translate(str.maketrans("áàâãéêíóôõúüç", "aaaaeeiooouuc"))


def ler_data(dia: str, mes: str, ano: str | None, ref: dt.date, depois_de: dt.date | None = None) -> dt.date:
    d, m = int(dia), int(mes)
    try:
        if ano:
            return dt.date(int(ano) + (2000 if len(ano) == 2 else 0), m, d)
        base = depois_de or ref
        data = dt.date(base.year, m, d)
        return data if data >= base else dt.date(base.year + 1, m, d)
    except ValueError:
        raise ValueError(f"Data inválida: {dia}/{mes}" + (f"/{ano}" if ano else "")) from None


def interpretar_periodo(texto: str, ref: dt.date, com_destino: bool = False) -> tuple[dict, str]:
    """Tira do texto as datas (dd/mm[/aaaa]) e os nomes de meses. Devolve (campos, texto que sobrou)."""
    campos: dict = {}
    datas = list(re.finditer(r"(\d{1,2})/(\d{1,2})(?:/(\d{4}|\d{2}))?", texto))
    if len(datas) not in (0, 2):
        raise ValueError("Use duas datas, ida e volta. Ex.: 01/09/2027 a 30/09/2027")
    if datas:
        d1 = ler_data(*datas[0].groups(), ref)
        d2 = ler_data(*datas[1].groups(), ref, depois_de=d1)
        if d2 <= d1:
            raise ValueError("A segunda data precisa ser depois da primeira.")
        if d1 <= ref:
            raise ValueError("A data de ida já passou.")
        campos["de"], campos["ate"] = d1.isoformat(), d2.isoformat()
        texto = texto[:datas[0].start()] + " " + texto[datas[0].end():datas[1].start()] + " " + texto[datas[1].end():]
    meses, resto = [], []
    viu_numero = not com_destino  # com destino, mês só vale depois dos dias ("Rio de Janeiro 5 em julho")
    for palavra in texto.split():
        chave = sem_acento(palavra.lower().strip(",."))
        viu_numero = viu_numero or bool(re.search(r"\d", chave))
        if viu_numero and chave in MESES_NOMES:
            meses.append(MESES_NOMES[chave])
        else:
            resto.append(palavra)
    if meses:
        campos["meses"] = sorted(set(meses))
    return campos, " ".join(resto)


RE_FAIXA = r"\b(\d{1,2})\s*(?:-|a|até|ate)\s*(\d{1,2})\b"


def interpretar_dias(texto: str) -> tuple[int, int] | None:
    m = re.search(RE_FAIXA, texto)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"\b(\d{1,2})\b", texto)
    return (int(m.group(1)), int(m.group(1))) if m else None


def validar_dias(dmin: int, dmax: int) -> None:
    if not (1 <= dmin <= dmax <= 60):
        raise ValueError("Duração inválida. Use de 1 a 60 dias, ex.: 11-16")


def buscar_destino(nome: str) -> dict:
    """Converte nome de cidade (ou código) em código IATA, pela busca pública da Travelpayouts."""
    nome = nome.strip()
    params = [("term", nome), ("locale", "pt"), ("types[]", "city"), ("types[]", "airport")]
    lista = get_json("https://autocomplete.travelpayouts.com/places2?" + urllib.parse.urlencode(params), timeout=30)
    if not lista:
        raise ValueError(f"Não achei o destino \"{nome}\". Tente o nome da cidade ou o código do aeroporto (ex.: MCO).")
    escolhido = lista[0]
    if re.fullmatch(r"[A-Za-z]{3}", nome):
        escolhido = next((x for x in lista if x["code"].upper() == nome.upper()), escolhido)
    aeroporto = escolhido["type"] == "airport"
    cidade = escolhido.get("city_name") if aeroporto else escolhido["name"]
    return {"codigo": escolhido["code"], "nome": cidade or escolhido["name"], "pais": escolhido.get("country_name") or "",
            "aeroporto": escolhido["name"] if aeroporto else (escolhido.get("main_airport_name") or "")}


def novo_id(nome: str, viagens: list[dict]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", sem_acento(nome.lower())).strip("-") or "viagem"
    ids, i, novo = {v["id"] for v in viagens}, 2, base
    while novo in ids:
        novo, i = f"{base}-{i}", i + 1
    return novo


def linha_viagem(i: int, v: dict) -> str:
    status = "✅" if v.get("ativa", True) else "⏸️ (pausada)"
    extra = f" · meta {brl(v['preco_alvo'])}" if v.get("preco_alvo") else ""
    return (f"{status} <b>{i}. {html.escape(v['nome'])}</b> ({v['origem']} → {v['destino']})\n"
            f"     {dias_txt(v)} · {periodo_txt(v)}\n"
            f"     {descricao_pax(v.get('passageiros', {}))}{extra}")


def msg_varredura_nova(v: dict, cfg: dict, ref: dt.date, tp_token: str) -> str:
    if not todas_combinacoes(v, cfg, ref):
        _, fim = periodo(cfg, ref)
        return (f"\n\n⏳ Nenhuma data possível agora. As companhias vendem até ~{data_br(fim)}; "
                "se o período for depois disso, começo a buscar quando ele entrar à venda.")
    if not tp_token:
        return ""
    try:
        cands = varrer_travelpayouts(v, cfg, ref, tp_token)
    except Exception as e:  # noqa: BLE001
        return f"\n\n(varredura falhou: {html.escape(str(e)[:100])})"
    if not cands:
        return "\n\nAinda não há preços em cache para essa rota. Vou sortear datas para conferir no Google."
    linhas = ["\n\n🔎 <b>Mais barato por mês</b> (estimativa por pessoa, Aviasales):"]
    for mes, c in resumo_varredura(cands)["por_mes"].items():
        linhas.append(f"   {mes_br(mes)}: {brl(c['preco_pp'])} ({data_br(c['ida'])} → {data_br(c['volta'])})")
    linhas.append("\nO preço total da família vem na busca do Google, todo dia às 8h.")
    return "\n".join(linhas)


def pegar_viagem(args: list[str], viagens: list[dict]) -> tuple[int, dict]:
    arg = args[0] if args else ""
    if not arg.isdigit() or not (1 <= int(arg) <= len(viagens)):
        raise ValueError("Número da viagem inválido. Veja os números em /viagens")
    return int(arg), viagens[int(arg) - 1]


def responder(texto: str, viagens: list[dict], cfg: dict, ref: dt.date, tp_token: str, hist: dict) -> tuple[str, bool]:
    """Interpreta uma mensagem do Telegram. Devolve (resposta, mudou_viagens)."""
    partes = texto.strip().split(maxsplit=1)
    cmd = sem_acento(partes[0].lower().lstrip("/").split("@")[0]) if partes else ""
    resto = partes[1] if len(partes) > 1 else ""
    # atalho: "/ushuaia 7" ou "Lisboa 10" vira "/nova ..."
    if cmd and cmd not in COMANDOS and re.search(r"\d", resto):
        cmd, resto = "nova", texto.strip().lstrip("/")
    args = resto.split()

    try:
        if cmd in ("start", "ajuda", "help", "menu", "comandos"):
            return AJUDA, False

        if cmd in ("viagens", "lista", "listar"):
            if not viagens:
                return "Nenhuma viagem cadastrada. Use /nova (veja /ajuda).", False
            return "🗺️ <b>Suas viagens</b>\n\n" + "\n\n".join(linha_viagem(i, v) for i, v in enumerate(viagens, 1)), False

        if cmd in ("precos", "resumo"):
            return msg_resumo([v for v in viagens if v.get("ativa", True)], hist, ref), False

        if cmd in ("nova", "novo", "viagem", "adicionar"):
            if not resto:
                return "Diga o destino e os dias. Ex.: <code>/nova Orlando 11-16</code>", False
            campos, sobra = interpretar_periodo(resto, ref, com_destino=True)
            dias = interpretar_dias(sobra)
            sobra = re.sub(RE_FAIXA + r"|\b\d{1,2}\b", " ", sobra)
            palavras = sobra.split()  # tira palavras soltas só das pontas ("Rio de Janeiro" fica inteiro)
            while palavras and sem_acento(palavras[0].lower()) in PALAVRAS_SOLTAS:
                palavras.pop(0)
            while palavras and sem_acento(palavras[-1].lower()) in PALAVRAS_SOLTAS:
                palavras.pop()
            nome = " ".join(palavras)
            if not nome:
                return "Faltou o destino. Ex.: <code>/nova Orlando 11-16</code>", False
            if dias is None:
                if not campos.get("de"):
                    return "Faltou a quantidade de dias. Ex.: <code>/nova Orlando 11-16</code>", False
                n = dias_entre(campos["de"], campos["ate"])
                dias = (n, n)
            validar_dias(*dias)
            if campos.get("de") and dias_entre(campos["de"], campos["ate"]) < dias[0]:
                return "⚠️ O período é menor que a quantidade de dias da viagem.", False
            dest = buscar_destino(nome)
            modelo = viagens[0] if viagens else {}
            apelido = nome.upper() if re.fullmatch(r"[A-Za-z]{3}", nome) else nome.title()
            v = {
                "id": novo_id(apelido, viagens),
                "nome": apelido,
                "origem": modelo.get("origem") or cfg["geral"].get("origem_padrao", "GRU"),
                "destino": dest["codigo"],
                "dias_min": dias[0],
                "dias_max": dias[1],
                "ativa": True,
                **campos,
                "passageiros": dict(modelo.get("passageiros") or {"adultos": 1}),
            }
            viagens.append(v)
            local = ", ".join(x for x in (dest["nome"], dest["pais"]) if x)
            if dest["aeroporto"] and dest["aeroporto"] != dest["nome"]:
                local += f" · aeroporto {dest['aeroporto']}"
            txt = (f"✅ <b>Viagem adicionada!</b>\n\n{linha_viagem(len(viagens), v)}\n"
                   f"     <i>{html.escape(local)}</i>\n\n"
                   f"Destino errado? Use <code>/remover {len(viagens)}</code> e mande de novo com o código do aeroporto.")
            return txt + msg_varredura_nova(v, cfg, ref, tp_token), True

        if cmd in ("remover", "apagar", "excluir"):
            i, v = pegar_viagem(args, viagens)
            viagens.pop(i - 1)
            return f"🗑️ {html.escape(v['nome'])} removida.", True

        if cmd in ("pausar", "ativar"):
            i, v = pegar_viagem(args, viagens)
            v["ativa"] = cmd == "ativar"
            return linha_viagem(i, v), True

        if cmd == "dias":
            i, v = pegar_viagem(args, viagens)
            dias = interpretar_dias(" ".join(args[1:]))
            if not dias:
                return "Ex.: <code>/dias 1 12-15</code>", False
            validar_dias(*dias)
            v["dias_min"], v["dias_max"] = dias
            return "Duração alterada:\n\n" + linha_viagem(i, v), True

        if cmd == "periodo":
            i, v = pegar_viagem(args, viagens)
            corpo = " ".join(args[1:])
            if sem_acento(corpo.lower()) in ("livre", "qualquer", "qualquer data", "tudo"):
                for k in ("de", "ate", "meses"):
                    v.pop(k, None)
                return "Período liberado:\n\n" + linha_viagem(i, v), True
            campos, _ = interpretar_periodo(corpo, ref)
            if not campos:
                return "Ex.: <code>/periodo 1 01/09/2027 a 30/09/2027</code> ou <code>/periodo 1 setembro</code>", False
            for k in ("de", "ate", "meses"):
                v.pop(k, None)
            v.update(campos)
            txt = "Período alterado:\n\n" + linha_viagem(i, v)
            if v.get("de") and dias_entre(v["de"], v["ate"]) < v["dias_min"]:
                txt += "\n\n⚠️ O período é menor que a duração. Ajuste com /dias"
            return txt + msg_varredura_nova(v, cfg, ref, tp_token), True

        if cmd == "pessoas":
            i, v = pegar_viagem(args, viagens)
            nums = [int(x) for x in args[1:] if x.isdigit()]
            if not nums or nums[0] < 1:
                return "Ex.: <code>/pessoas 1 2 1</code> (2 adultos, 1 criança). Bebê de colo é o 3º número.", False
            v["passageiros"] = {"adultos": nums[0], "criancas": nums[1] if len(nums) > 1 else 0,
                                "bebes_colo": nums[2] if len(nums) > 2 else 0, "bebes_assento": 0}
            return "Passageiros alterados:\n\n" + linha_viagem(i, v), True

        if cmd == "meta":
            i, v = pegar_viagem(args, viagens)
            valor = re.sub(r"\D", "", args[1].split(",")[0]) if len(args) > 1 else ""
            if not valor:
                return "Ex.: <code>/meta 1 15000</code> (0 tira a meta)", False
            if int(valor) == 0:
                v.pop("preco_alvo", None)
            else:
                v["preco_alvo"] = int(valor)
            return "Meta alterada:\n\n" + linha_viagem(i, v), True
    except ValueError as e:
        return f"⚠️ {html.escape(str(e))}", False
    except RuntimeError as e:
        return f"⚠️ Não consegui consultar agora ({html.escape(str(e)[:120])}). Tente de novo mais tarde.", False

    return "Não entendi. 🤔\n\n" + AJUDA, False


def rodar_bot(cfg: dict, ref: dt.date, tp_token: str) -> int:
    token = os.environ.get("TELEGRAM_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not (token and chat):
        print("Defina TELEGRAM_TOKEN e TELEGRAM_CHAT_ID.")
        return 1
    estado = json.loads(ARQ_BOT.read_text(encoding="utf-8")) if ARQ_BOT.exists() else {}
    params = {"timeout": 0, "allowed_updates": json.dumps(["message"])}
    if estado.get("offset"):
        params["offset"] = estado["offset"]
    dados = get_json(f"https://api.telegram.org/bot{token}/getUpdates?" + urllib.parse.urlencode(params), timeout=30)
    updates = dados.get("result") or []
    print(f"{len(updates)} mensagens novas")
    if not updates:
        return 0
    viagens = carregar_viagens(cfg)
    hist = carregar_hist(ARQ_HIST)
    mudou = False
    for up in updates:
        estado["offset"] = up["update_id"] + 1
        msg = up.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != chat:
            print("ignorada: mensagem de outro chat")
            continue
        texto = msg.get("text") or ""
        print(f"> {texto[:80]}")
        resposta, alterou = responder(texto, viagens, cfg, ref, tp_token, hist)
        mudou = mudou or alterou
        telegram(resposta, True)
    if mudou:
        salvar_viagens(viagens)
    ARQ_BOT.parent.mkdir(parents=True, exist_ok=True)
    ARQ_BOT.write_text(json.dumps(estado), encoding="utf-8")
    return 0


# --------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="Monitor de passagens")
    ap.add_argument("--simular", action="store_true", help="usa preços inventados (não gasta buscas)")
    ap.add_argument("--telegram", action="store_true", help="no modo simular, envia também no Telegram")
    ap.add_argument("--varredura", action="store_true", help="só roda a varredura da Travelpayouts e mostra o resultado")
    ap.add_argument("--listar", action="store_true", help="só mostra o período e as combinações de datas")
    ap.add_argument("--testar-telegram", action="store_true", help="envia uma mensagem de teste")
    ap.add_argument("--bot", action="store_true", help="lê e responde as mensagens do Telegram")
    ap.add_argument("--comando", help="testa um comando do bot localmente (salva em data/viagens.json)")
    args = ap.parse_args()

    cfg = carregar_config()
    ref = hoje()

    if args.testar_telegram:
        if not (os.environ.get("TELEGRAM_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")):
            print("Defina TELEGRAM_TOKEN e TELEGRAM_CHAT_ID.")
            return 1
        telegram("✅ <b>Teste OK!</b>\nO monitor de passagens consegue te mandar avisos aqui.", True)
        return 0

    tp_token = os.environ.get("TRAVELPAYOUTS_TOKEN", "")
    if args.bot:
        return rodar_bot(cfg, ref, tp_token)
    if args.comando is not None:
        todas = carregar_viagens(cfg)
        resposta, mudou = responder(args.comando, todas, cfg, ref, tp_token, carregar_hist(ARQ_HIST))
        print(resposta)
        if mudou:
            salvar_viagens(todas)
        return 0

    viagens = [v for v in carregar_viagens(cfg) if v.get("ativa", True)]

    if args.listar:
        inicio, fim = periodo(cfg, ref)
        print(f"Período: ida a partir de {data_br(inicio)}, volta até {data_br(fim)}")
        for v in viagens:
            print(f"{v['nome']}: {len(todas_combinacoes(v, cfg, ref))} combinações, {dias_txt(v)}, {periodo_txt(v)}")
        return 0

    if args.varredura:
        if not tp_token:
            print("Falta a variável TRAVELPAYOUTS_TOKEN.")
            return 1
        t = tp_token.strip()
        print(f"Token: {len(t)} caracteres, formato {'ok (32 hex)' if re.fullmatch(r'[0-9a-f]{32}', t) else 'inesperado'}")
        for v in viagens:
            cands = varrer_travelpayouts(v, cfg, ref, tp_token)
            res = resumo_varredura(cands)
            print(f"\n{v['nome']}: {res['candidatos']} combinações válidas")
            for mes, c in res["por_mes"].items():
                print(f"   {mes_br(mes)}: {brl(c['preco_pp'])} por pessoa ({data_br(c['ida'])} → {data_br(c['volta'])})")
            for c in cands[:5]:
                print(f"   top: {brl(c['preco_pp'])} {data_br(c['ida'])} → {data_br(c['volta'])} {c['companhia']}")
        return 0

    chave = os.environ.get("SERPAPI_KEY", "")
    if not args.simular and not chave:
        print("Falta a variável SERPAPI_KEY (ou use --simular).")
        return 1

    arq_hist = ARQ_HIST_SIM if args.simular else ARQ_HIST
    hist = carregar_hist(arq_hist)
    migrar_historico(hist, viagens)
    enviar = (not args.simular) or args.telegram
    erros = []

    # 1. varredura
    candidatos = {}
    for v in viagens:
        try:
            if args.simular:
                cands = varrer_simulado(v, cfg, ref)
            elif tp_token:
                cands = varrer_travelpayouts(v, cfg, ref, tp_token)
            else:
                cands = []
        except Exception as e:  # noqa: BLE001
            erros.append(f"Varredura {v['nome']}: {e}")
            print("   ERRO varredura:", e)
            cands = []
        candidatos[v["id"]] = cands
        if cands:
            hist["varredura"][v["id"]] = resumo_varredura(cands)
        print(f"Varredura {v['nome']}: {len(cands)} combinações com preço")

    # 2. conferência no Google: divide as buscas entre as viagens
    orcamento = cfg["geral"].get("buscas_por_execucao", 7)
    rodada = hist.get("rodada", 0)
    com_datas = [v for v in viagens if candidatos[v["id"]] or todas_combinacoes(v, cfg, ref)]
    cotas = {v["id"]: 0 for v in viagens}
    for v in com_datas:
        cotas[v["id"]] = orcamento // len(com_datas)
    for i in range(orcamento - sum(cotas.values()) if com_datas else 0):
        cotas[com_datas[(rodada + i) % len(com_datas)]["id"]] += 1
    hist["rodada"] = rodada + 1

    total_buscas = 0
    for v in viagens:
        est = hist["viagens"].setdefault(v["id"], {"observacoes": []})
        escolhidas = escolher_para_conferir(v, candidatos[v["id"]], est, cfg, ref, cotas[v["id"]], set())
        for ida, volta, origem in escolhidas:
            total_buscas += 1
            print(f"Conferindo {v['nome']} ({origem}): {data_br(ida)} → {data_br(volta)} ...")
            try:
                res = buscar_simulado(v, ida, volta) if args.simular else buscar_serpapi(v, ida, volta, chave)
            except Exception as e:  # noqa: BLE001
                erros.append(f"{v['nome']} {data_br(ida)}: {e}")
                print("   ERRO:", e)
                continue
            if res is None:
                print("   sem voos para essa data")
                continue
            obs = {"ida": ida, "volta": volta, "em": agora_iso(), **res}
            print(f"   {brl(obs['preco'])} · {obs['companhias']} · Google: {obs.get('nivel_google')}")

            motivos = avaliar(est, obs, v, cfg, ref)
            est["observacoes"].append(obs)
            est["observacoes"] = est["observacoes"][-MAX_OBS_POR_VIAGEM:]
            if not est.get("recorde") or obs["preco"] < est["recorde"]["preco"]:
                est["recorde"] = obs
            if motivos:
                telegram(msg_alerta(v, obs, motivos), enviar)
                est["ultimo_alerta"] = {"preco": obs["preco"], "em": obs["em"]}

    # avisa problemas (ex.: cota da SerpApi acabou), no máximo 1x por dia
    if erros and hist.get("ultimo_erro_avisado") != ref.isoformat():
        telegram("⚠️ <b>Monitor de passagens com problema</b>\n" + html.escape("\n".join(erros[:5])), enviar)
        hist["ultimo_erro_avisado"] = ref.isoformat()

    dia_resumo = cfg["geral"].get("dia_resumo_semanal", 0)
    if dia_resumo >= 0 and ref.weekday() == dia_resumo and hist.get("ultimo_resumo") != ref.isoformat():
        telegram(msg_resumo(viagens, hist, ref), enviar)
        hist["ultimo_resumo"] = ref.isoformat()

    salvar_hist(arq_hist, hist)
    gerar_relatorio(viagens, hist, cfg, ref, RAIZ / "RELATORIO_simulado.md" if args.simular else ARQ_RELATORIO)
    print(f"\nPronto: {total_buscas} buscas no Google, {len(erros)} erros.")
    # só falha a execução se TODAS as buscas deram erro
    return 1 if total_buscas and len(erros) >= total_buscas else 0


if __name__ == "__main__":
    sys.exit(main())
