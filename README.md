# Teste Técnico — Desenvolvedor Sênior de Automação e Integração

## Estrutura do projeto

```
.
├── parte1_rpa/
│   └── rpa_challenge.py       # Automação RPA Challenge (Playwright)
├── parte2_hacker_news/
│   ├── database.py            # SQLite: schema, upsert, watermark, fila de falhas
│   ├── loader.py              # Carga incremental com retry/backoff
│   └── reporter.py            # Relatório do estado local
├── tests/
│   ├── test_database.py       # Idempotência, watermark, fila de falhas
│   ├── test_loader.py         # Carga incremental, retry, resumo
│   └── test_rpa_mapping.py    # Parse Excel, mapeamento de campos (sem browser)
├── artifacts/                 # Screenshots e JSON de evidência do RPA
├── data/                      # Banco SQLite gerado em execução
└── requirements.txt
```

---

## Como rodar (início rápido)

```powershell
# 1. Clone o repositório
git clone https://github.com/alexandrofs31/teste-senior-automacao
cd teste-senior-automacao

# 2. Instale as dependências
pip install -r requirements.txt
playwright install chromium

# 3. Crie o arquivo de variáveis de ambiente
Copy-Item .env.example .env   # PowerShell
# cp .env.example .env        # Linux/Mac

# 4. Pronto — rode a Parte 1 ou Parte 2 conforme as seções abaixo
```

> O arquivo `.env` não é versionado (`.gitignore`). Os valores padrão do `.env.example` já apontam para as URLs públicas corretas — nenhuma configuração adicional é necessária para rodar o projeto.

---

## Parte 1 — RPA Challenge

### Execução

```bash
# Headless (padrão, indicado para CI)
python -m parte1_rpa.rpa_challenge

# Com browser visível
python -m parte1_rpa.rpa_challenge --no-headless
```

### Evidências geradas

| Arquivo | Conteúdo |
|---|---|
| `artifacts/rpa_result.png` | Screenshot da tela de resultado final |
| `artifacts/rpa_evidence.json` | Métricas: registros, acurácia, duração |

### Decisões técnicas

**Playwright** foi escolhido sobre Selenium por:

- Auto-wait nativo: não há `time.sleep()` frágil; a biblioteca espera o elemento estar interativo antes de agir.
- API async moderna com download management integrado.
- Suporte a headless/não-headless sem configuração adicional.

**Identificação de campos**: usa `page.locator('input[ng-reflect-name="..."]')` — atributo Angular estável que identifica cada campo independente da posição visual. É exatamente esse reposicionamento dinâmico dos campos que o RPA Challenge testa, e o `ng-reflect-name` garante 100% de acurácia em todas as 10 rodadas.

**Limitações conhecidas**:
- Se o rpachallenge.com estiver fora do ar ou mudar radicalmente o HTML, a automação precisará de ajuste nos seletores de "Download" e "Start".
- Execução headless requer Chromium instalado (`playwright install chromium`).

---

## Parte 2 — Carga incremental Hacker News

### Execução

```bash
# 1ª execução (carga inicial, padrão: últimos 200 itens)
python -m parte2_hacker_news.loader

# Carga inicial com tamanho customizado
python -m parte2_hacker_news.loader --batch-size 500

# 2ª+ execução (incremental — detectada automaticamente pelo watermark)
python -m parte2_hacker_news.loader

# Relatório do banco local
python -m parte2_hacker_news.reporter
```

### Fluxo incremental

```
1ª execução:
  max_id = GET /maxitem.json           → ex: 42.000.000
  start_id = max_id - batch_size + 1  → ex: 41.999.801
  Processa IDs 41.999.801 → 42.000.000
  Salva watermark = 42.000.000

2ª execução:
  max_id = GET /maxitem.json           → ex: 42.000.050
  start_id = watermark + 1            → ex: 42.000.001
  Processa apenas IDs novos: 42.000.001 → 42.000.050
  Atualiza watermark = 42.000.050
```

### Idempotência

`upsert_item()` usa `INSERT OR REPLACE` (chave primária = `id`). Reexecutar a carga sobre o mesmo range não cria duplicatas — no máximo atualiza campos se o item mudou.

### Tratamento de falhas

- **Retry automático**: `requests` com `Retry(total=3, backoff_factor=0.5)` para erros 5xx e timeouts de rede. Backoff exponencial: 0 s, 0,5 s, 1 s.
- **Fila de falhas**: IDs que falharam após todos os retries vão para a tabela `failed_items`. Na próxima execução, são reprocessados antes dos IDs novos.
- **Itens null**: itens deletados da API retornam `null` — são contados como "ignorados" e não bloqueiam o watermark.
- **Watermark por maior sucesso**: avança até o maior ID processado com sucesso, mesmo que haja IDs falhos no intervalo. Os IDs falhos ficam registrados em `failed_items` e são reprocessados na próxima execução — trade-off deliberado entre progresso do pipeline e rastreabilidade de falhas.

### Schema SQLite

```sql
items        -- dados persistidos (id PK, campos consultáveis, raw_json)
state        -- watermark: last_item_id
failed_items -- fila de IDs com erro, com contador de tentativas
```

### Decisões técnicas

**SQLite**: zero configuração, stdlib do Python, adequado ao volume da demo. Para produção com múltiplos workers, migrar para PostgreSQL.

**requests + HTTPAdapter**: mais simples que httpx/aiohttp para este caso síncrono; retry declarativo via `urllib3.util.retry.Retry`.

**raw_json**: o JSON bruto do item é preservado integralmente, garantindo que campos futuros da API não sejam perdidos.

**Limitações conhecidas**:
- Não é thread-safe; SQLite com journal em memória não garante consistência sob escrita concorrente.
- IDs que falham persistentemente (ex: item permanentemente indisponível) acumulam na fila; não há TTL de expiração — melhoria futura.
- A carga inicial não retroage ao início do histórico do HN (42 M+ itens). Isso é intencional — o objetivo é demonstrar o padrão incremental.

---

## Testes

```bash
pytest tests/ -v
```

Cobertura focada nos riscos relevantes:
- **Idempotência**: inserir o mesmo item duas vezes não duplica.
- **Watermark**: avança corretamente entre execuções.
- **Itens null**: não causam inserção nem erros.
- **Fila de falhas**: IDs com erro são enfileirados e reprocessados.
- **Mapeamento RPA**: parse de Excel e mapeamento coluna→label sem browser.

---

## Uso de IA

Claude (Anthropic) foi utilizado para estruturar e escrever o código. Todas as decisões de arquitetura (seleção de biblioteca, estratégia de watermark, tratamento de falhas) foram avaliadas tecnicamente e são defensáveis pelo candidato.
