#!/usr/bin/env python3
"""story-reminder: gera ideias de stories de Instagram com IA e envia no Telegram.

Pensado para a La Motta Patissier: 6 lembretes por dia, cada um com 2 ideias
prontas para gravar só com o celular em menos de 2 minutos.

Uso:
    python story_reminder.py                 # escolhe o slot pelo horário atual
    python story_reminder.py --slot tarde_1  # força um slot
    python story_reminder.py --dry-run       # só imprime, não envia nem grava histórico
    python story_reminder.py --get-chat-id   # lista os chat_ids que falaram com o bot
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

# --------------------------------------------------------------------------
# Configuração
# --------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
BRAND_PATH = BASE_DIR / "brand.md"
HISTORY_PATH = BASE_DIR / "history.json"

DEFAULT_LLM_URL = "https://opencode.ai/inference/go/openai/v1/chat/completions"
DEFAULT_LLM_MODEL = "deepseek-v4.1-flash"
DEFAULT_TZ = "America/Sao_Paulo"

MAX_TENTATIVAS = 3          # tentativas por chamada ao LLM (5xx, 429, rede)
ESPERA_BASE_S = 5           # espera = ESPERA_BASE_S * tentativa
MAX_HISTORICO = 300         # entradas guardadas em history.json
RECENTES_NO_PROMPT = 60     # ideias recentes enviadas ao modelo como "NÃO repetir"
LIMITE_TELEGRAM = 4096      # tamanho máximo de uma mensagem do Telegram
USER_AGENT = "story-reminder/1.0"

CAMPOS_IDEIA = ("formato", "titulo", "o_que_gravar", "texto_na_tela", "sticker")

DIAS_SEMANA = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
               "sexta-feira", "sábado", "domingo"]

# Cada slot: horário-alvo (São Paulo), rótulo, foco e formatos possíveis.
SLOTS: dict[str, dict] = {
    "manha_1": {
        "hora": "08:00",
        "label": "Manhã — abrir o dia",
        "foco": "bom dia com personalidade, rotina, café/cozinha",
        "formatos": ["bom dia", "o que estou fazendo agora",
                     "bastidor de produção", "enquete leve"],
    },
    "manha_2": {
        "hora": "10:30",
        "label": "Manhã — mão na massa",
        "foco": "processo, ingredientes, close/ASMR",
        "formatos": ["passo a passo rápido", "ASMR/close",
                     "ingrediente do dia", "caixinha de perguntas"],
    },
    "tarde_1": {
        "hora": "13:30",
        "label": "Tarde — vender",
        "foco": "produto do dia, sabor, disponibilidade, como encomendar",
        "formatos": ["vitrine do produto", "enquete de sabor",
                     "cardápio/encomenda", "quiz"],
    },
    "tarde_2": {
        "hora": "16:00",
        "label": "Tarde — engajamento",
        "foco": "caixinha de perguntas, isso ou aquilo, curiosidades",
        "formatos": ["caixinha de perguntas", "isso ou aquilo",
                     "mito ou verdade", "curiosidade"],
    },
    "noite_1": {
        "hora": "19:00",
        "label": "Noite — prova social",
        "foco": "depoimento, entrega, embalagem, história da marca",
        "formatos": ["repost de cliente", "unboxing/embalagem",
                     "história da marca", "bastidor humano"],
    },
    "noite_2": {
        "hora": "21:00",
        "label": "Noite — fechamento",
        "foco": "prévia do amanhã, aviso de encomendas, boa noite",
        "formatos": ["prévia do amanhã", "contagem de encomendas", "boa noite",
                     'enquete "o que quer ver amanhã"'],
    },
}


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------

def exigir_env(nome: str) -> str:
    """Lê uma variável de ambiente obrigatória ou falha com mensagem clara."""
    valor = os.environ.get(nome, "").strip()
    if not valor:
        raise RuntimeError(f"variável de ambiente {nome} não definida")
    return valor


def agora(tz_name: str | None = None) -> datetime:
    return datetime.now(ZoneInfo(tz_name or os.environ.get("TZ_NAME") or DEFAULT_TZ))


def _minutos(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def pick_slot(now: datetime) -> str:
    """Escolhe o slot cujo horário-alvo está mais próximo de `now`.

    A distância é circular (dá a volta na meia-noite), assim um atraso grande do
    cron do GitHub ainda cai no slot certo (ex.: 21:50 -> noite_2).
    """
    atual = now.hour * 60 + now.minute

    def distancia(slot: str) -> int:
        d = abs(atual - _minutos(SLOTS[slot]["hora"]))
        return min(d, 24 * 60 - d)

    return min(SLOTS, key=distancia)


def _limpar_segredo(texto: str, *segredos: str) -> str:
    """Remove segredos de um texto (defesa extra para mensagens de erro)."""
    for s in segredos:
        if s:
            texto = texto.replace(s, "***")
    return texto


# --------------------------------------------------------------------------
# Marca e histórico
# --------------------------------------------------------------------------

def carregar_brand(path: Path | None = None) -> str:
    path = path or BRAND_PATH
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return "(brand.md não encontrado — use bom senso para uma confeitaria artesanal)"


def carregar_historico(path: Path | None = None) -> list[dict]:
    """Lê history.json; arquivo ausente ou corrompido vira lista vazia."""
    path = path or HISTORY_PATH
    try:
        dados = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(dados, list):
        return []
    return [e for e in dados if isinstance(e, dict)]


def salvar_historico(novas: list[dict], path: Path | None = None) -> None:
    path = path or HISTORY_PATH
    historico = (carregar_historico(path) + novas)[-MAX_HISTORICO:]
    path.write_text(json.dumps(historico, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------

def montar_system_prompt(brand: str) -> str:
    return f"""Você é social media de uma confeitaria artesanal brasileira.
Sua tarefa é sugerir ideias de stories de Instagram fáceis de executar em menos \
de 2 minutos, só com o celular, sem equipamento nem edição complicada.
Tom: caloroso, próximo, de quem conversa com o cliente — nada de linguagem corporativa.

## Contexto da marca
{brand}

## Regras
- Gere EXATAMENTE 2 ideias, de formatos DIFERENTES entre si.
- Não repita nem parafraseie as ideias recentes listadas pelo usuário.
- Cada ideia diz o que filmar, o texto que vai na tela e o sticker a usar.
- Caixinhas de perguntas, enquetes e quizzes já vêm com a pergunta e as opções prontas.
- Cada campo tem no máximo 2 frases.
- Considere o dia da semana e datas comemorativas próximas.
- Responda APENAS com JSON válido, sem texto antes ou depois, neste formato:
{{"ideias":[{{"formato":"...","titulo":"...","o_que_gravar":"...","texto_na_tela":"...","sticker":"..."}}]}}"""


def montar_user_prompt(now: datetime, slot: str, historico: list[dict]) -> str:
    info = SLOTS[slot]
    recentes = historico[-RECENTES_NO_PROMPT:]
    if recentes:
        linhas = "\n".join(
            f"- [{e.get('formato', '')}] {e.get('titulo', '')}" for e in recentes
        )
    else:
        linhas = "- (nenhuma ainda)"
    return f"""Agora: {DIAS_SEMANA[now.weekday()]}, {now:%d/%m/%Y %H:%M}.
Momento do dia: {info['label']}
Foco: {info['foco']}
Formatos possíveis (escolha 2 diferentes): {', '.join(info['formatos'])}

Ideias recentes (NÃO repetir):
{linhas}"""


# --------------------------------------------------------------------------
# LLM
# --------------------------------------------------------------------------

def chamar_llm(system: str, user: str, *, api_key: str, model: str, url: str,
               now: datetime) -> str:
    """Chama o endpoint OpenAI-compatível e devolve o texto da resposta.

    Faz até MAX_TENTATIVAS para 5xx, 429 e erros de rede. Outros 4xx falham na
    hora. A chave nunca entra na mensagem de erro.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "User-Agent": USER_AGENT,
        "x-opencode-session": f"story-reminder-{now:%Y-%m-%d}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": 2000,
        "temperature": 0.9,
    }

    ultimo_erro = "erro desconhecido"
    for tentativa in range(1, MAX_TENTATIVAS + 1):
        try:
            resp = httpx.post(url, headers=headers, json=body, timeout=90)
        except httpx.HTTPError as e:
            ultimo_erro = f"falha de rede ao chamar o LLM ({type(e).__name__})"
        else:
            if resp.status_code == 200:
                try:
                    dados = resp.json()
                    conteudo = dados["choices"][0]["message"].get("content")
                except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                    trecho = _limpar_segredo(resp.text[:300], api_key)
                    raise RuntimeError(f"resposta inesperada do LLM: {trecho}")
                return conteudo or ""
            trecho = _limpar_segredo(resp.text[:300], api_key)
            ultimo_erro = f"LLM respondeu HTTP {resp.status_code}: {trecho}"
            if resp.status_code != 429 and resp.status_code < 500:
                raise RuntimeError(ultimo_erro)
        if tentativa < MAX_TENTATIVAS:
            time.sleep(ESPERA_BASE_S * tentativa)
    raise RuntimeError(f"{ultimo_erro} (após {MAX_TENTATIVAS} tentativas)")


def parse_ideas(texto: str) -> list[dict]:
    """Extrai as ideias do texto do modelo (tolera ```json e texto ao redor)."""
    inicio, fim = texto.find("{"), texto.rfind("}")
    if inicio == -1 or fim <= inicio:
        raise ValueError("nenhum objeto JSON na resposta")
    dados = json.loads(texto[inicio:fim + 1])
    ideias = dados.get("ideias") if isinstance(dados, dict) else None
    if not isinstance(ideias, list) or not ideias:
        raise ValueError('campo "ideias" ausente ou vazio')
    resultado = []
    for item in ideias:
        if not isinstance(item, dict):
            continue
        resultado.append({
            c: str(item.get(c) or "").strip() for c in CAMPOS_IDEIA
        })
        if len(resultado) == 2:
            break
    if not resultado:
        raise ValueError('"ideias" não contém objetos válidos')
    return resultado


def gerar_ideias(now: datetime, slot: str, historico: list[dict], *,
                 api_key: str, model: str, url: str) -> list[dict]:
    """Gera as ideias; se o JSON vier inválido, refaz a chamada uma vez."""
    system = montar_system_prompt(carregar_brand())
    user = montar_user_prompt(now, slot, historico)
    erro = None
    for _ in range(2):
        texto = chamar_llm(system, user, api_key=api_key, model=model, url=url, now=now)
        try:
            return parse_ideas(texto)
        except ValueError as e:
            erro = e
    raise RuntimeError(f"o modelo não devolveu um JSON válido após 2 chamadas: {erro}")


# --------------------------------------------------------------------------
# Mensagem e Telegram
# --------------------------------------------------------------------------

def truncar_html(texto: str, limite: int = LIMITE_TELEGRAM) -> str:
    """Corta a mensagem no limite do Telegram sem deixar tag ou entidade quebrada."""
    if len(texto) <= limite:
        return texto
    corte = texto[:limite - 10]
    corte = re.sub(r"<[^>]*$", "", corte)        # tag pela metade
    corte = re.sub(r"&[#\w]*$", "", corte)       # entidade pela metade (&amp…)
    for tag in ("i", "b"):
        if corte.count(f"<{tag}>") > corte.count(f"</{tag}>"):
            corte += f"</{tag}>"
    return corte + "…"


def formatar_mensagem(slot: str, ideias: list[dict], *, html_mode: bool = True) -> str:
    """Monta a mensagem. Em html_mode, todo texto do modelo é escapado."""
    info = SLOTS[slot]
    esc = html.escape if html_mode else (lambda s: s)
    b = (lambda s: f"<b>{s}</b>") if html_mode else (lambda s: s)
    i = (lambda s: f"<i>{s}</i>") if html_mode else (lambda s: s)

    linhas = [f"📲 {b('Hora do story!')} — {esc(info['label'])} ({info['hora']})"]
    for n, ideia in enumerate(ideias, 1):
        linhas += [
            "",
            f"{b(f'{n}. ' + esc(ideia['titulo']))} {i('(' + esc(ideia['formato']) + ')')}",
            f"🎥 {esc(ideia['o_que_gravar'])}",
            f"✍️ Texto: {esc(ideia['texto_na_tela'])}",
            f"🏷️ Sticker: {esc(ideia['sticker'])}",
        ]
    texto = "\n".join(linhas)
    return truncar_html(texto) if html_mode else texto


def _telegram(token: str, metodo: str, payload: dict | None = None) -> dict:
    """Chama a API do Telegram. O token nunca aparece nas mensagens de erro."""
    url = f"https://api.telegram.org/bot{token}/{metodo}"
    try:
        resp = httpx.post(url, json=payload or {}, timeout=30)
    except httpx.HTTPError as e:
        # Não usar str(e): a exceção pode carregar a URL, que contém o token.
        raise RuntimeError(f"falha de rede ao chamar o Telegram ({type(e).__name__})") from None
    try:
        dados = resp.json()
    except ValueError:
        raise RuntimeError(f"Telegram respondeu HTTP {resp.status_code} sem JSON") from None
    if not dados.get("ok"):
        descricao = _limpar_segredo(str(dados.get("description", "sem descrição")), token)
        raise RuntimeError(f"Telegram recusou ({resp.status_code}): {descricao}")
    return dados


def enviar_telegram(texto: str, *, token: str, chat_id: str) -> None:
    _telegram(token, "sendMessage", {
        "chat_id": chat_id,
        "text": texto,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })


def listar_chat_ids(token: str) -> list[dict]:
    """Lê getUpdates e devolve os chats encontrados (sem repetição)."""
    dados = _telegram(token, "getUpdates")
    chats: dict[int, dict] = {}
    for update in dados.get("result", []):
        for chave in ("message", "channel_post", "my_chat_member"):
            chat = (update.get(chave) or {}).get("chat")
            if chat and "id" in chat:
                nome = chat.get("title") or " ".join(
                    p for p in (chat.get("first_name"), chat.get("last_name")) if p
                ) or chat.get("username") or ""
                chats[chat["id"]] = {"id": chat["id"], "tipo": chat.get("type", ""),
                                     "nome": nome}
    return list(chats.values())


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Lembretes de stories com ideias geradas por IA.")
    p.add_argument("--slot", default="auto", choices=["auto", *SLOTS],
                   help="slot do dia (padrão: auto, pelo horário atual)")
    p.add_argument("--dry-run", action="store_true",
                   help="gera e imprime em texto puro, sem enviar nem gravar histórico")
    p.add_argument("--get-chat-id", action="store_true",
                   help="lista os chat_ids que mandaram mensagem para o bot")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.get_chat_id:
        chats = listar_chat_ids(exigir_env("TELEGRAM_BOT_TOKEN"))
        if not chats:
            print("Nenhum chat encontrado. Mande /start para o bot (ou uma mensagem "
                  "no grupo) e rode de novo.")
        for c in chats:
            print(f"chat_id={c['id']}  tipo={c['tipo']}  nome={c['nome']}")
        return 0

    api_key = exigir_env("OPENCODE_API_KEY")
    if not args.dry_run:
        token = exigir_env("TELEGRAM_BOT_TOKEN")
        chat_id = exigir_env("TELEGRAM_CHAT_ID")
    model = os.environ.get("LLM_MODEL", "").strip() or DEFAULT_LLM_MODEL
    url = os.environ.get("LLM_URL", "").strip() or DEFAULT_LLM_URL

    now = agora()
    slot = pick_slot(now) if args.slot == "auto" else args.slot
    historico = carregar_historico()
    ideias = gerar_ideias(now, slot, historico, api_key=api_key, model=model, url=url)

    if args.dry_run:
        print(formatar_mensagem(slot, ideias, html_mode=False))
        return 0

    enviar_telegram(formatar_mensagem(slot, ideias), token=token, chat_id=chat_id)
    # Só grava o histórico depois que o envio deu certo.
    salvar_historico([
        {"data": now.isoformat(timespec="minutes"), "slot": slot,
         "formato": ideia["formato"], "titulo": ideia["titulo"]}
        for ideia in ideias
    ])
    print(f"Enviado: {slot} ({len(ideias)} ideias).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:
        print(f"ERRO: {e}", file=sys.stderr)
        sys.exit(1)
