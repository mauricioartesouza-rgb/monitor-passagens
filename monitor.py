#!/usr/bin/env python3
"""
Monitor de passagens aéreas -> avisos no iPhone via Telegram.

Fonte dos preços: Google Flights (via SerpApi), que reúne as tarifas das
companhias aéreas e das agências reais.

Uso:
  python monitor.py                     busca normal (precisa das chaves)
  python monitor.py --simular           testa tudo com preços inventados
  python monitor.py --listar            mostra as datas que serão pesquisadas
  python monitor.py --testar-telegram   manda uma mensagem de teste

Variáveis de ambiente:
  SERPAPI_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

Não precisa instalar nada além do Python 3.11+.
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import html
import json
import os
import random
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
FUSO_BR = dt.timezone(dt.timedelta(hours=-3))
MAX_OBS_POR_VIAGEM = 600


# --------------------------------------------------------------------- util
def hoje() -> dt.date:
    return dt.datetime.now(FUSO_BR).date()


def agora_iso() -> str:
    return dt.datetime.now(FUSO_BR).isoformat(timespec="minutes")


def brl(valor: float) -> str:
    return "R$ " + f"{valor:,.0f}".replace(",", ".")


def data_br(d: str | dt.date) -> str:
    if isinstance(d, str):
        d = dt.date.fromisoformat(d)
    return d.strftime("%d/%m/%Y")


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


def carregar_config() -> dict:
    with open(ARQ_CONFIG, "rb") as f:
        return tomllib.load(f)


def carregar_hist(caminho: Path) -> dict:
    if caminho.exists():
        with open(caminho, encoding="utf-8") as f:
            return json.load(f)
    return {"versao": 1, "rodada": 0, "cursores": {}, "viagens": {}}


def salvar_hist(caminho: Path, hist: dict) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with open(caminho, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=1)


# ------------------------------------------------------------ datas a buscar
def janela(viagem: dict, ref: dt.date):
    meses = viagem["meses"]
    m1, m2 = min(meses), max(meses)
    anos = [viagem["ano"]] if "ano" in viagem else [ref.year, ref.year + 1, ref.year + 2]
    for ano in anos:
        inicio = dt.date(ano, m1, 1)
        fim = dt.date(ano, m2, calendar.monthrange(ano, m2)[1])
        if fim - dt.timedelta(days=viagem["dias"]) > ref:
            return inicio, fim
    return None


def pares_de_datas(viagem: dict, cfg: dict, ref: dt.date):
    """Retorna (lista de (ida, volta), observação)."""
    j = janela(viagem, ref)
    if not j:
        return [], "período já passou"
    inicio, fim = j
    dur = viagem["dias"]
    passo = max(1, viagem.get("passo_dias", 3))
    max_ant = cfg["geral"].get("dias_max_antecedencia", 330)
    limite_venda = ref + dt.timedelta(days=max_ant)
    ultima_ida = fim - dt.timedelta(days=dur)

    pares = []
    ida = inicio
    while ida <= ultima_ida:
        volta = ida + dt.timedelta(days=dur)
        if ida > ref and volta <= limite_venda:
            pares.append((ida, volta))
        ida += dt.timedelta(days=passo)

    if pares:
        return pares, f"{len(pares)} combinações de datas"
    abre = inicio + dt.timedelta(days=dur) - dt.timedelta(days=max_ant)
    return [], f"vendas ainda não abertas (começa a buscar por volta de {data_br(abre)})"


# ------------------------------------------------------------------- buscas
STOPS = {None: 0, 0: 1, 1: 2, 2: 3}  # parâmetro "stops" da SerpApi


def buscar_serpapi(viagem: dict, ida: dt.date, volta: dt.date, chave: str) -> dict | None:
    pax = viagem.get("passageiros", {})
    params = {
        "engine": "google_flights",
        "departure_id": viagem["origem"],
        "arrival_id": viagem["destino"],
        "outbound_date": ida.isoformat(),
        "return_date": volta.isoformat(),
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
    url = "https://serpapi.com/search.json?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=90) as r:
            dados = json.load(r)
    except urllib.error.HTTPError as e:
        corpo = e.read().decode("utf-8", "replace")
        try:
            msg = json.loads(corpo).get("error", corpo)
        except json.JSONDecodeError:
            msg = corpo
        raise RuntimeError(f"SerpApi HTTP {e.code}: {msg[:300]}") from None

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


def link_google(viagem: dict, ida: dt.date, volta: dt.date) -> str:
    q = f"Flights from {viagem['origem']} to {viagem['destino']} on {ida.isoformat()} through {volta.isoformat()}"
    return "https://www.google.com/travel/flights?hl=pt-BR&curr=BRL&q=" + urllib.parse.quote(q)


def buscar_simulado(viagem: dict, ida: dt.date, volta: dt.date) -> dict:
    pax = viagem.get("passageiros", {})
    pagantes = pax.get("adultos", 1) + pax.get("criancas", 0) + pax.get("bebes_assento", 0)
    base = 5200 if viagem["destino"] == "MCO" else 2600
    preco = max(800, random.gauss(base, base * 0.18)) * pagantes + pax.get("bebes_colo", 0) * base * 0.1
    tipico = [round(base * 0.85 * pagantes), round(base * 1.2 * pagantes)]
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
def avaliar(estado: dict, obs: dict, viagem: dict, cfg: dict) -> list[str]:
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
        dias = (hoje() - dt.date.fromisoformat(ultimo["em"][:10])).days
        if obs["preco"] >= ultimo["preco"] and dias < 7:
            return []
    return motivos


def msg_alerta(viagem: dict, obs: dict, motivos: list[str]) -> str:
    escalas = "voo direto" if obs["escalas"] == 0 else f"{obs['escalas']} escala{'s' if obs['escalas'] > 1 else ''}"
    return (
        f"✈️ <b>{html.escape(viagem['nome'])}</b>: {brl(obs['preco'])}\n"
        f"👨‍👩‍👦 total para {descricao_pax(viagem.get('passageiros', {}))}\n"
        f"📅 {data_br(obs['ida'])} → {data_br(obs['volta'])} ({viagem['dias']} dias)\n"
        f"🛫 {html.escape(obs['companhias'])} · {escalas} · ida {duracao_txt(obs['duracao_min'])}\n\n"
        + "\n".join(motivos)
        + f"\n\n<a href=\"{html.escape(obs['link'])}\">👉 Ver e comprar no Google Flights</a>\n"
        "<i>Preços mudam rápido. Confira o valor final antes de pagar.</i>"
    )


def msg_resumo(viagens: list[dict], hist: dict, planos: dict) -> str:
    linhas = ["📊 <b>Resumo semanal das passagens</b>", "O robô está funcionando ✅", ""]
    corte = (hoje() - dt.timedelta(days=7)).isoformat()
    for v in viagens:
        est = hist["viagens"].get(v["id"], {})
        linhas.append(f"<b>{html.escape(v['nome'])}</b>")
        rec = est.get("recorde")
        if not rec:
            linhas.append(f"   {planos[v['id']][1]}")
        else:
            semana = [o for o in est.get("observacoes", []) if o["em"] >= corte]
            linhas.append(f"   Menor já visto: {brl(rec['preco'])} ({data_br(rec['ida'])} → {data_br(rec['volta'])})")
            if semana:
                m = min(semana, key=lambda o: o["preco"])
                linhas.append(f"   Melhor desta semana: {brl(m['preco'])} ({data_br(m['ida'])})")
            linhas.append(f"   Buscas feitas: {len(est.get('observacoes', []))}")
        linhas.append("")
    return "\n".join(linhas).strip()


# ---------------------------------------------------------------- relatório
def gerar_relatorio(viagens: list[dict], hist: dict, planos: dict, caminho: Path) -> None:
    out = ["# ✈️ Relatório de passagens", "", f"_Atualizado em {agora_iso().replace('T', ' ')} (horário de Brasília)_", ""]
    for v in viagens:
        est = hist["viagens"].get(v["id"], {})
        obs = est.get("observacoes", [])
        out += [f"## {v['nome']}", "",
                f"{v['origem']} → {v['destino']} · {v['dias']} dias · {descricao_pax(v.get('passageiros', {}))}  ",
                f"Situação: {planos[v['id']][1]}", ""]
        rec = est.get("recorde")
        if rec:
            out += [f"**Menor preço já visto: {brl(rec['preco'])}** ({data_br(rec['ida'])} → {data_br(rec['volta'])}, "
                    f"{rec['companhias']}, visto em {rec['em'][:10]}) · [abrir]({rec['link']})", ""]
            # melhor preço atual por data de ida (última vez que cada data foi vista)
            atual = {}
            for o in obs:
                atual[o["ida"]] = o
            out += ["| Ida | Volta | Total | Companhia | Escalas | Google | Visto em |",
                    "|---|---|---|---|---|---|---|"]
            for ida in sorted(atual):
                o = atual[ida]
                nivel = {"low": "🟢 baixo", "typical": "🟡 normal", "high": "🔴 alto"}.get(o.get("nivel_google"), "-")
                out.append(f"| {data_br(o['ida'])} | {data_br(o['volta'])} | [{brl(o['preco'])}]({o['link']}) | "
                           f"{o['companhias']} | {o['escalas']} | {nivel} | {o['em'][:10]} |")
            out.append("")
    caminho.write_text("\n".join(out), encoding="utf-8")


# --------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="Monitor de passagens")
    ap.add_argument("--simular", action="store_true", help="usa preços inventados (não gasta buscas)")
    ap.add_argument("--telegram", action="store_true", help="no modo simular, envia também no Telegram")
    ap.add_argument("--listar", action="store_true", help="só mostra as datas que serão pesquisadas")
    ap.add_argument("--testar-telegram", action="store_true", help="envia uma mensagem de teste")
    args = ap.parse_args()

    cfg = carregar_config()
    ref = hoje()

    if args.testar_telegram:
        if not (os.environ.get("TELEGRAM_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")):
            print("Defina TELEGRAM_TOKEN e TELEGRAM_CHAT_ID.")
            return 1
        telegram("✅ <b>Teste OK!</b>\nO monitor de passagens consegue te mandar avisos aqui.", True)
        return 0

    viagens = [v for v in cfg.get("viagens", []) if v.get("ativa", True)]
    planos = {v["id"]: pares_de_datas(v, cfg, ref) for v in viagens}

    if args.listar:
        for v in viagens:
            pares, nota = planos[v["id"]]
            print(f"\n{v['nome']}: {nota}")
            for ida, volta in pares:
                print(f"   {data_br(ida)} → {data_br(volta)}")
        return 0

    chave = os.environ.get("SERPAPI_KEY", "")
    if not args.simular and not chave:
        print("Falta a variável SERPAPI_KEY (ou use --simular).")
        return 1

    arq_hist = ARQ_HIST_SIM if args.simular else ARQ_HIST
    hist = carregar_hist(arq_hist)
    enviar = (not args.simular) or args.telegram

    # escolhe as buscas desta execução: revezando entre as viagens e
    # andando pelas datas de cada uma, para cobrir tudo com poucas buscas
    ativas = [v for v in viagens if planos[v["id"]][0]]
    orcamento = cfg["geral"].get("buscas_por_execucao", 3)
    total_pares = sum(len(planos[v["id"]][0]) for v in ativas)
    escolhidas, ja = [], set()
    rodada = hist.get("rodada", 0)
    tentativas = 0
    while ativas and len(escolhidas) < min(orcamento, total_pares) and tentativas < orcamento * 4:
        v = ativas[(rodada + tentativas) % len(ativas)]
        tentativas += 1
        pares = planos[v["id"]][0]
        c = hist["cursores"].get(v["id"], 0)
        hist["cursores"][v["id"]] = c + 1
        ida, volta = pares[c % len(pares)]
        if (v["id"], ida) in ja:
            continue
        ja.add((v["id"], ida))
        escolhidas.append((v, ida, volta))
    hist["rodada"] = rodada + tentativas

    erros = []
    for v, ida, volta in escolhidas:
        print(f"Buscando {v['nome']}: {data_br(ida)} → {data_br(volta)} ...")
        try:
            res = buscar_simulado(v, ida, volta) if args.simular else buscar_serpapi(v, ida, volta, chave)
        except Exception as e:  # noqa: BLE001
            erros.append(f"{v['nome']} {data_br(ida)}: {e}")
            print("   ERRO:", e)
            continue
        if res is None:
            print("   sem voos para essa data")
            continue
        obs = {"ida": ida.isoformat(), "volta": volta.isoformat(), "em": agora_iso(), **res}
        print(f"   {brl(obs['preco'])} · {obs['companhias']} · Google: {obs.get('nivel_google')}")

        est = hist["viagens"].setdefault(v["id"], {"observacoes": []})
        motivos = avaliar(est, obs, v, cfg)
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
        telegram(msg_resumo(viagens, hist, planos), enviar)
        hist["ultimo_resumo"] = ref.isoformat()

    salvar_hist(arq_hist, hist)
    gerar_relatorio(viagens, hist, planos, RAIZ / "RELATORIO_simulado.md" if args.simular else ARQ_RELATORIO)
    print(f"\nPronto: {len(escolhidas)} buscas, {len(erros)} erros.")
    # só falha a execução se TODAS as buscas deram erro
    return 1 if escolhidas and len(erros) == len(escolhidas) else 0


if __name__ == "__main__":
    sys.exit(main())
