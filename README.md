# ✈️ Monitor de Passagens: GRU → Orlando e Bariloche

Um robô que todo dia procura as passagens mais baratas para **Orlando** e **Bariloche**, em **qualquer mês do ano**, para viagens de **11 a 16 dias**, e te avisa no **iPhone pelo Telegram** quando:

- 🏆 aparece o **menor preço já visto** desde que ele começou a monitorar;
- 📉 o próprio Google Flights classifica o preço como **baixo** para a rota;
- 🎯 o preço fica abaixo de uma **meta** sua (opcional).

Toda segunda-feira ele manda um **resumo semanal**. Cada aviso traz o link da passagem no Google Flights.

Não precisa deixar computador ligado: ele roda de graça nos servidores do GitHub.

---

## Como ele procura

1. **Varredura (Travelpayouts/Aviasales):** todo dia, olha os preços mais baratos já encontrados para cada mês dos próximos ~10 meses, em todas as datas com 11 a 16 dias. É grátis e cobre o ano inteiro, mas é uma estimativa para 1 adulto.
2. **Conferência (Google Flights/SerpApi):** pega as datas mais baratas da varredura e confere no Google Flights com a família toda (2 adultos + 1 criança). É esse preço total que vale para os avisos.
3. **Aviso:** se o preço conferido for bom, manda no Telegram com o link.

Sem a Travelpayouts, o robô continua funcionando, mas sorteia as datas para conferir.

---

## Para que serve cada serviço

| Serviço | Papel | Custo |
|---|---|---|
| **Telegram** | Onde chegam os avisos, como notificação normal no iPhone. | Grátis |
| **Travelpayouts** | Programa de parceiros da Aviasales (desde 2011). Dá os preços do ano inteiro para a varredura. | Grátis |
| **SerpApi** | Entrega os resultados do Google Flights. Confere o preço real da família. | Grátis até 250 buscas/mês |
| **GitHub** | Onde o robô fica e roda sozinho todo dia. Guarda o histórico de preços. | Grátis |

Você **não** compra nada por eles: o aviso traz um link do Google Flights, onde você escolhe comprar direto na companhia ou numa agência.

---

## Passo a passo

### 1. Criar o bot do Telegram (no iPhone)

1. Instale o **Telegram** pela App Store e crie sua conta.
2. Procure **@BotFather** (selo azul de verificado) e abra a conversa.
3. Envie `/newbot`. Escolha um nome e depois um usuário terminado em `bot`.
4. Ele responde com um **token** parecido com `123456789:AAH...`. Esse é o `TELEGRAM_TOKEN`.
5. Abra a conversa com o seu bot novo e mande um **"oi"**.
6. Num navegador, abra `https://api.telegram.org/botSEU_TOKEN/getUpdates` (trocando `SEU_TOKEN`). Anote o número depois de `"chat":{"id":`. Esse é o `TELEGRAM_CHAT_ID`.

### 2. Pegar a chave da SerpApi

1. Crie uma conta grátis em **https://serpapi.com**.
2. No painel, vá em **Api Key** e copie a chave. Essa é a `SERPAPI_KEY`.

### 3. Pegar o token da Travelpayouts

1. Crie uma conta grátis em **https://www.travelpayouts.com** (botão *Join* / *Sign up*).
2. Se pedir um site ou projeto, pode cadastrar o endereço do seu repositório no GitHub e escolher "uso pessoal / outro".
3. Entre no programa **Aviasales** (se pedir).
4. Abra **https://www.travelpayouts.com/programs/100/tools/api** e copie o **API token**. Esse é o `TRAVELPAYOUTS_TOKEN`.

### 4. Colocar no GitHub

1. Envie os arquivos desta pasta para o repositório privado `monitor-passagens`.
2. Em **Settings → Secrets and variables → Actions → New repository secret**, crie:
   - `SERPAPI_KEY`
   - `TRAVELPAYOUTS_TOKEN`
   - `TELEGRAM_TOKEN`
   - `TELEGRAM_CHAT_ID`

### 5. Testar

Na aba **Actions → Monitor de passagens → Run workflow**, escolha:

- **testar-telegram**: manda "✅ Teste OK!" no Telegram.
- **testar-varredura**: mostra no log os preços da Travelpayouts por mês (não gasta SerpApi).
- **simular**: manda avisos com **preços inventados**, só para ver o formato.
- **buscar**: a busca real. Depois abra o **RELATORIO.md**.
- **bot**: lê e responde agora as mensagens do Telegram.

A partir daí ele roda sozinho **todo dia às 8h**.

---

## Mudar as viagens pelo Telegram

Mande mensagem para o seu bot. Ele lê as mensagens a cada ~10 minutos e responde. Comandos:

| Mensagem | O que faz |
|---|---|
| `/nova Orlando 11-16` | nova viagem, qualquer data, 11 a 16 dias |
| `/nova Bariloche 12 em setembro` | ida em setembro, 12 dias |
| `/nova Paris 10-14 de 01/03/2027 a 30/04/2027` | dentro desse período |
| `/nova Miami 10/03/2027 a 25/03/2027` | datas exatas |
| `/viagens` | lista as viagens com o número de cada uma |
| `/dias 1 12-15` | muda a duração da viagem 1 |
| `/periodo 1 01/09/2027 a 30/09/2027` | muda o período (`/periodo 1 livre` volta para qualquer data) |
| `/pessoas 1 2 1` | 2 adultos e 1 criança (3º número: bebê de colo) |
| `/meta 1 15000` | avisa quando o total ficar abaixo de R$ 15.000 |
| `/pausar 1`, `/ativar 1`, `/remover 1` | pausa, reativa ou apaga |
| `/precos` | resumo dos preços |
| `/ajuda` | mostra os comandos |

O bot só obedece o seu chat (`TELEGRAM_CHAT_ID`). As viagens ficam salvas em `data/viagens.json`.
Quanto mais viagens ativas, menos buscas no Google cada uma recebe por dia (são 7 no total).

## Ajustes no `config.toml`

Edite direto no GitHub (abra o arquivo e clique no lápis ✏️):

- **Mais buscas por dia**: aumente `buscas_por_execucao`. O plano grátis da SerpApi tem 250 por mês (7 por dia já usa ~210).
- **Só voo direto ou no máximo 1 escala**: adicione `"escalas_max": 0` ou `1` na viagem, em `data/viagens.json`.

## Dicas

- Os avisos de "menor preço histórico" só começam depois de 10 buscas por viagem. Os de "preço baixo segundo o Google" funcionam desde o primeiro dia.
- O preço da varredura é por pessoa e pode estar alguns dias desatualizado. O que vale é o total conferido no Google.
- Preço de passagem muda em minutos. Quando chegar um aviso bom, abra o link logo e confira o valor final antes de pagar.
