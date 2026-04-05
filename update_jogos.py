#!/usr/bin/env python3
"""
Atualiza dados de jogos do futebol sergipano 2026.
Usa Gemini 2.0 Flash com Google Search grounding (plano pago).

Modos:
  --mode morning  → busca resultados de jogos das últimas 48h
  --mode midday   → busca confirmações de datas para jogos "a_definir"
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta

from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    print("❌ GEMINI_API_KEY não encontrada.")
    sys.exit(1)

client = genai.Client(api_key=GEMINI_API_KEY)

DATA_FILE = "data/jogos.json"
HTML_FILE = "index.html"
MARKER_START = "/*__JOGOS_START__*/"
MARKER_END   = "/*__JOGOS_END__*/"


# ── I/O ───────────────────────────────────────────────────────────────────────

def load_data() -> dict:
    with open(DATA_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_data(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def rebuild_html(jogos: list, updated_at: str):
    with open(HTML_FILE, encoding="utf-8") as f:
        html = f.read()

    def to_js_obj(j: dict) -> str:
        fields = []
        for k, v in j.items():
            val = f'"{v}"' if isinstance(v, str) else str(v).lower()
            fields.append(f'{k}:{val}')
        return "  { " + ", ".join(fields) + " }"

    jogos_js = "[\n" + ",\n".join(to_js_obj(j) for j in jogos) + "\n]"

    pattern = re.escape(MARKER_START) + r"[\s\S]*?" + re.escape(MARKER_END)
    new_html = re.sub(pattern, MARKER_START + jogos_js + MARKER_END, html)

    new_html = re.sub(
        r'(id="updated-at">)([^<]+)',
        rf'\1Atualizado em {updated_at}',
        new_html,
    )

    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(new_html)


# ── GEMINI ────────────────────────────────────────────────────────────────────

def call_gemini(prompt: str) -> str:
    """Gemini 2.0 Flash com Google Search grounding."""
    config = types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())],
    )
    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=config,
            )
            return response.text
        except Exception as e:
            print(f"  Tentativa {attempt + 1} falhou: {e}")
            if attempt < 2:
                time.sleep(8 * (attempt + 1))
            else:
                raise


def extract_json(text: str) -> list:
    """Extrai o primeiro array JSON encontrado no texto."""
    match = re.search(r'```(?:json)?\s*(\[[\s\S]*?\])\s*```', text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    match = re.search(r'\[[\s\S]*?\]', text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    raise ValueError(f"JSON não encontrado na resposta:\n{text[:400]}")


# ── MORNING: resultados ───────────────────────────────────────────────────────

def morning_update(data: dict) -> dict:
    today = datetime.now()
    cutoff = today - timedelta(days=2)

    def parse_date(s):
        try:
            return datetime.strptime(s, "%d/%m/%Y")
        except Exception:
            return None

    candidates = [
        j for j in data["jogos"]
        if j["status"] == "agendado"
        and (d := parse_date(j["data"])) is not None
        and cutoff <= d <= today
    ]

    if not candidates:
        print("ℹ️  Nenhum jogo recente para verificar resultados.")
        return data

    games_list = "\n".join(
        f"- {j['time']} x {j['adversario']} | {j['comp']} | {j['data']} {j['horario']}"
        for j in candidates
    )

    prompt = f"""Você é especialista em futebol brasileiro. Hoje é {today.strftime('%d/%m/%Y')}.

Busque na web os resultados dos seguintes jogos de times sergipanos:

{games_list}

Para cada jogo com resultado CONFIRMADO, retorne:
[{{
  "time": "<nome exato do time sergipano>",
  "adversario": "<nome exato>",
  "data": "<DD/MM/YYYY>",
  "resultado": "<X×Y>",
  "status": "realizado"
}}]

Se decidido nos pênaltis: "X×Y (pên. A×B)".
Inclua SOMENTE jogos com resultado já divulgado.
Retorne APENAS o array JSON, sem texto adicional."""

    print("🔍 Buscando resultados de jogos recentes...")
    response_text = call_gemini(prompt)
    print(f"   Resposta:\n{response_text[:500]}")

    try:
        updates = extract_json(response_text)
    except ValueError as e:
        print(f"⚠️  {e}")
        return data

    updated = 0
    for upd in updates:
        for jogo in data["jogos"]:
            if (
                jogo["time"] == upd.get("time")
                and jogo["adversario"] == upd.get("adversario")
                and jogo["data"] == upd.get("data")
                and jogo["status"] == "agendado"
            ):
                jogo["status"] = "realizado"
                jogo["resultado"] = upd.get("resultado", "")
                updated += 1
                print(f"  ✅ {jogo['time']} {jogo['resultado']} {jogo['adversario']} ({jogo['comp']})")

    print(f"📊 Resultados atualizados: {updated}")
    return data


# Times sergipanos monitorados (usados na varredura proativa por time)
TIMES_SERGIPANOS = [
    "Sergipe FC", "Confiança", "Itabaiana", "América de Propriá", "Lagarto",
    "Falcon", "Desportiva Aracaju", "Atlético Gloriense", "Dorense", "Guarany-SE",
]


# ── MIDDAY: calendário ────────────────────────────────────────────────────────

def midday_update(data: dict) -> dict:
    today = datetime.now()
    updated = 0

    # ── Passo 1: confirmar datas de jogos já mapeados (a_definir) ────────────
    undefined = [j for j in data["jogos"] if j["status"] == "a_definir"]

    if undefined:
        games_list = "\n".join(
            f"- {j['time']} x {j['adversario']} | {j['comp']} | {j['fase']}"
            for j in undefined
        )
        prompt = f"""Você é especialista em futebol brasileiro. Hoje é {today.strftime('%d/%m/%Y')}.

Busque na web se as datas dos seguintes jogos de times sergipanos em 2026 já foram confirmadas:

{games_list}

Para cada jogo com data CONFIRMADA, retorne:
[{{
  "time": "<nome exato do time sergipano>",
  "adversario": "<nome exato>",
  "comp": "<nome da competição>",
  "data": "<DD/MM/YYYY>",
  "horario": "<HH:MM ou 'A def.'>",
  "local": "<Estádio — Cidade/UF ou 'A definir'>",
  "status": "agendado"
}}]

Inclua SOMENTE jogos com data oficial confirmada. Retorne APENAS o array JSON."""

        print("🔍 Passo 1: confirmando datas de jogos existentes...")
        try:
            resp = call_gemini(prompt)
            print(f"   Resposta:\n{resp[:400]}")
            for upd in extract_json(resp):
                for jogo in data["jogos"]:
                    if (
                        jogo["time"] == upd.get("time")
                        and jogo["adversario"] == upd.get("adversario")
                        and jogo["comp"] == upd.get("comp")
                        and jogo["status"] == "a_definir"
                    ):
                        jogo["data"]    = upd.get("data",    jogo["data"])
                        jogo["horario"] = upd.get("horario", jogo["horario"])
                        jogo["local"]   = upd.get("local",   jogo["local"])
                        jogo["status"]  = "agendado"
                        updated += 1
                        print(f"  📅 {jogo['time']} x {jogo['adversario']}: {jogo['data']} {jogo['horario']}")
        except Exception as e:
            print(f"  ⚠️  {e}")
    else:
        print("ℹ️  Nenhum jogo sem data para verificar.")

    # ── Passo 2: varredura por time — detectar novas competições ─────────────
    jogos_existentes = {
        (j["time"], j["adversario"], j["comp"]) for j in data["jogos"]
    }
    comps_existentes = {
        (j["time"], j["comp"]) for j in data["jogos"]
    }

    print("\n🔍 Passo 2: varredura por time para detectar novas competições...")
    for nome_time in TIMES_SERGIPANOS:
        comps_do_time = [comp for t, comp in comps_existentes if t == nome_time]
        comps_txt = "\n".join(f"- {c}" for c in comps_do_time) or "- nenhuma"

        prompt = f"""Você é especialista em futebol brasileiro. Hoje é {today.strftime('%d/%m/%Y')}.

Busque na web: o time "{nome_time}" (de Sergipe) está disputando alguma competição em 2026?

Competições que JÁ ESTÃO mapeadas para este time (ignorar):
{comps_txt}

Se encontrar competições ou jogos NOVOS não listados acima, retorne:
[{{
  "time": "{nome_time}",
  "comp": "<nome da competição>",
  "badge": "<seriod|seriec|brasil|nordeste|sergipao>",
  "fase": "<fase ou grupo>",
  "mando": "<casa|fora|a_def>",
  "adversario": "<adversário>",
  "data": "<DD/MM/YYYY ou 'A confirmar'>",
  "horario": "<HH:MM ou 'A conf.'>",
  "local": "<Estádio — Cidade/UF ou 'A confirmar'>",
  "status": "<agendado|a_definir>"
}}]

Se não há novidades, retorne: []
Retorne APENAS o array JSON."""

        time.sleep(4)  # respeitar rate limit
        try:
            resp = call_gemini(prompt)
            novos = extract_json(resp)
            for novo in novos:
                chave = (novo.get("time"), novo.get("adversario"), novo.get("comp"))
                if chave not in jogos_existentes and novo.get("adversario"):
                    data["jogos"].append(novo)
                    jogos_existentes.add(chave)
                    comps_existentes.add((novo.get("time"), novo.get("comp")))
                    updated += 1
                    print(f"  🆕 {novo['time']} | {novo['comp']} | {novo.get('adversario')} ({novo.get('data')})")
        except Exception as e:
            print(f"  ⚠️  {nome_time}: {e}")

    print(f"\n📆 Total de atualizações: {updated}")
    return data


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["morning", "midday"], required=True)
    args = parser.parse_args()

    data = load_data()
    print(f"📂 {len(data['jogos'])} jogos | último update: {data.get('updatedAt', '?')}")

    if args.mode == "morning":
        data = morning_update(data)
    else:
        data = midday_update(data)

    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    data["updatedAt"] = now
    save_data(data)
    rebuild_html(data["jogos"], now)
    print(f"\n✅ Concluído: {now}")


if __name__ == "__main__":
    main()
