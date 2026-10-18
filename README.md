# story-reminder

Lembrete no Telegram, 6 vezes por dia, com 2 ideias de stories de Instagram prontas
para gravar só com o celular em menos de 2 minutos. Feito para a **La Motta Patissier**.

As ideias são geradas por IA (OpenCode Go, modelo DeepSeek), levando em conta o
contexto da marca ([brand.md](brand.md)), o momento do dia, o dia da semana e o que já
foi sugerido antes (`history.json`), para não repetir. Tudo roda de graça no
GitHub Actions — não precisa de servidor.

## Horários

| São Paulo | Slot      | Foco                                   | Cron (UTC)    |
|-----------|-----------|----------------------------------------|---------------|
| 08:03     | `manha_1` | Abrir o dia: bom dia, rotina, café     | `3 11 * * *`  |
| 10:33     | `manha_2` | Mão na massa: processo, close/ASMR     | `33 13 * * *` |
| 13:33     | `tarde_1` | Vender: produto do dia, encomendas     | `33 16 * * *` |
| 16:03     | `tarde_2` | Engajamento: perguntas, isso ou aquilo | `3 19 * * *`  |
| 19:03     | `noite_1` | Prova social: clientes, embalagem      | `3 22 * * *`  |
| 21:03     | `noite_2` | Fechamento: prévia do amanhã, boa noite| `3 0 * * *`   |

O GitHub costuma atrasar os crons de alguns minutos (às vezes mais). Por isso o
script escolhe o slot pelo horário-alvo **mais próximo** da hora em que de fato rodou.

## Passo a passo

### 1. Criar o bot no Telegram

1. No Telegram, abra o **@BotFather**, mande `/newbot` e siga as instruções.
2. Guarde o **token** que ele devolve (algo como `123456:ABC...`). Ele é secreto.
3. Abra a conversa com o seu bot novo e mande `/start`.

**Opção: grupo com a esposa.** Para os dois receberem os lembretes, crie um grupo,
adicione o bot e mande qualquer mensagem no grupo (ex.: `/start@nome_do_bot`).

### 2. Descobrir o chat_id

Com Python 3.10+ na sua máquina:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # preencha TELEGRAM_BOT_TOKEN
set -a && . ./.env && set +a
python story_reminder.py --get-chat-id
```

Vai aparecer algo como `chat_id=123456789 tipo=private nome=...`. Chat de grupo
tem id negativo (ex.: `-1001234567890`). Se não aparecer nada, mande outra mensagem
para o bot e rode de novo.

**Dicas:** no Telegram, deixe a conversa do bot **fixada** no topo e configure um
**som de notificação próprio** para ela (nas configurações de notificação do chat) —
assim dá para saber, sem olhar, que é hora do story.

### 3. Criar o repositório no GitHub (PRIVADO)

Crie um repositório **privado** e suba estes arquivos. Privado porque o
`history.json` e o `brand.md` falam do seu negócio. (Repositórios privados têm
2.000 minutos/mês grátis de Actions; este projeto usa algo como 150–200.)

### 4. Cadastrar os secrets

No repositório: **Settings → Secrets and variables → Actions → New repository secret**.

| Secret               | Valor                                   |
|----------------------|-----------------------------------------|
| `OPENCODE_API_KEY`   | Chave da API do OpenCode Go             |
| `TELEGRAM_BOT_TOKEN` | Token do @BotFather                     |
| `TELEGRAM_CHAT_ID`   | O número que o `--get-chat-id` mostrou  |

**Opcional:** na aba **Variables**, crie `LLM_MODEL` para trocar o modelo
(padrão `deepseek-v4.1-flash`; alternativa mais caprichada: `deepseek-v4-pro`).

### 5. Preencher o brand.md

Abra o [brand.md](brand.md) e troque os exemplos entre parênteses pelas informações
reais da marca. Quanto mais específico, melhores as ideias. Atualize as datas
sazonais de vez em quando.

### 6. Testar

1. Vá em **Actions → Stories → Run workflow**, marque **dry_run** e rode. As ideias
   aparecem no log do passo "Gerar e enviar ideias", sem enviar nada.
2. Rode de novo **sem** dry_run. A mensagem deve chegar no Telegram e o
   `history.json` ganha um commit do `github-actions[bot]`.

Depois disso, os 6 horários rodam sozinhos todos os dias.

## Rodar localmente

```bash
set -a && . ./.env && set +a
python story_reminder.py --dry-run               # só imprime
python story_reminder.py --slot tarde_1 --dry-run
python story_reminder.py                         # envia de verdade e grava history.json
```

Variáveis opcionais: `LLM_MODEL`, `LLM_URL` (endpoint OpenAI-compatível) e
`TZ_NAME` (padrão `America/Sao_Paulo`).

## Observações

- **Autenticação do OpenCode Go:** o script manda `Authorization: Bearer <chave>`,
  que é o padrão das APIs compatíveis com OpenAI. A documentação do OpenCode Go não
  detalha esse header; se aparecer **erro 401**, confira a chave e o formato de
  autenticação no console do OpenCode.
- O `history.json` guarda as últimas 300 ideias. Pode apagar o arquivo para
  "zerar a memória" — o script aceita arquivo ausente ou corrompido.
- Erros aparecem no log do Actions como `ERRO: ...`; chaves e tokens nunca são
  impressos.
