# ✈️ Monitor de Passagens: GRU → Orlando e Bariloche

Um robô que todo dia pesquisa preços no **Google Flights** e te avisa no **iPhone pelo Telegram** quando:

- 🏆 aparece o **menor preço já visto** desde que ele começou a monitorar;
- 📉 o próprio Google Flights classifica o preço como **baixo** para a rota (com base no histórico dele);
- 🎯 o preço fica abaixo de uma **meta** sua (opcional).

Toda segunda-feira ele manda um **resumo semanal**, para você saber que continua funcionando.

Não precisa deixar computador ligado: ele roda de graça nos servidores do GitHub.

---

## Para que serve cada serviço

| Serviço | Papel | Custo |
|---|---|---|
| **Telegram** | App de mensagens. O robô te manda os avisos por ele, como uma notificação normal no iPhone. | Grátis |
| **SerpApi** | Empresa americana (desde 2016) que entrega os resultados do Google Flights para programas. É a "fonte" dos preços. | Grátis até 250 buscas/mês |
| **GitHub** | Site da Microsoft onde o robô fica guardado e roda sozinho todo dia (GitHub Actions). Também guarda o histórico de preços. | Grátis |

Você **não** compra nada por eles: o aviso traz um link do Google Flights, onde você escolhe comprar direto na companhia (LATAM, GOL, Azul, Aerolíneas, American…) ou numa agência.

---

## Passo a passo (cerca de 20 minutos)

### 1. Criar o bot do Telegram (no iPhone)

1. Instale o **Telegram** pela App Store e crie sua conta.
2. Na busca, procure **@BotFather** (tem selo azul de verificado) e abra a conversa.
3. Envie `/newbot`. Escolha um nome (ex.: `Passagens da Família`) e depois um usuário terminado em `bot` (ex.: `passagens_familia_bot`).
4. Ele responde com um **token** parecido com `123456789:AAH...`. **Guarde esse token** (é o `TELEGRAM_TOKEN`).
5. Abra a conversa com o seu bot novo (o link `t.me/...` que o BotFather mostrou) e mande um **"oi"**.
6. Num navegador, abra o endereço abaixo, trocando `SEU_TOKEN` pelo token:
   `https://api.telegram.org/botSEU_TOKEN/getUpdates`
   Procure `"chat":{"id":` e anote o número que vem depois (ex.: `987654321`). Esse é o `TELEGRAM_CHAT_ID`.
   *Se aparecer `"result":[]`, mande outra mensagem para o bot e recarregue a página.*

### 2. Pegar a chave da SerpApi

1. Crie uma conta grátis em **https://serpapi.com** (botão *Register*).
2. Confirme o e-mail e o telefone.
3. No painel, vá em **Api Key** e copie a chave (é a `SERPAPI_KEY`).

### 3. Colocar o robô no GitHub

1. Crie uma conta em **https://github.com**.
2. Clique em **+ → New repository**. Nome: `monitor-passagens`. Marque **Private**. Clique em **Create repository**.
3. Na página do repositório, clique em **uploading an existing file** e **arraste todos os arquivos desta pasta**, incluindo a pasta `.github` e o `.gitignore`. Clique em **Commit changes**.
   > ⚠️ No Windows, pastas que começam com ponto (`.github`) às vezes ficam ocultas. Se o arquivo `.github/workflows/monitor.yml` não subir, crie pelo site: **Add file → Create new file**, digite o nome `.github/workflows/monitor.yml` e cole o conteúdo.
4. Vá em **Settings → Secrets and variables → Actions → New repository secret** e crie três segredos:
   - `SERPAPI_KEY` → chave da SerpApi
   - `TELEGRAM_TOKEN` → token do BotFather
   - `TELEGRAM_CHAT_ID` → número do chat

### 4. Testar

1. Vá na aba **Actions** do repositório. Se pedir, clique para habilitar os workflows.
2. Clique em **Monitor de passagens → Run workflow**, escolha **testar-telegram** e clique em **Run workflow**. Em menos de um minuto deve chegar "✅ Teste OK!" no Telegram.
3. Rode de novo escolhendo **simular**. Chegam avisos com **preços inventados** (só para ver o formato; não gasta buscas).
4. Rode de novo escolhendo **buscar**. É a busca real. Depois abra o arquivo **RELATORIO.md** no repositório para ver a tabela de preços.

A partir daí ele roda sozinho **todo dia às 8h**.

---

## Como funciona a busca

- As janelas da Orlando (fev–abr, 15 dias) e da Bariloche (setembro, 12 dias) viram uma lista de combinações de ida e volta (Orlando a cada 3 dias, Bariloche a cada 2).
- Para caber no plano grátis (250 buscas/mês), ele faz **7 buscas por dia**, alternando as viagens e avançando nas datas. Em cerca de 5 dias ele cobre todas as datas e recomeça.
- O preço é o **total ida e volta para todos os passageiros**, com taxas, em reais.
- As viagens de **2028** já estão configuradas. O robô começa a pesquisá-las sozinho quando as vendas abrirem (~11 meses antes).
- Quando uma viagem passar, ela é ignorada automaticamente.

## Ajustes no `config.toml`

Edite direto no GitHub (abra o arquivo e clique no lápis ✏️):

- **Passageiros**: `adultos`, `criancas` (2 a 11 anos), `bebes_colo` (menos de 2 anos). Vale a idade **na data da volta**.
- **Meta de preço**: tire o `#` da linha `preco_alvo` e coloque o valor total.
- **Mais buscas por dia**: aumente `buscas_por_execucao`. Mas o plano grátis da SerpApi tem 250 buscas por mês (7 por dia já usa ~210); acima disso precisa assinar um plano.
- **Só voo direto ou no máximo 1 escala**: adicione `escalas_max = 0` ou `escalas_max = 1` na viagem.

## Dicas

- Os avisos de "menor preço histórico" só começam depois de 10 buscas por viagem. Antes disso, qualquer preço seria um "recorde". Os avisos de "preço baixo segundo o Google" funcionam desde o primeiro dia.
- Preço de passagem muda em minutos. Quando chegar um aviso bom, abra o link logo e confira o valor final antes de pagar.
- Comprar direto no site da companhia costuma facilitar remarcação e problemas.
