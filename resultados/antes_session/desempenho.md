# Desempenho REST x SOAP

- Data/hora: 11/11/2026 09:10
- Estabelecimento: CNES 4884124
- Chamadas por serviço: 10 (intercaladas, pausa de 0.5 s, após 1 chamada de aquecimento)
- Tempo: da chamada da função até ter os dados em Python (rede + processamento). "Só rede" = envio, espera e download do HTTP (só medido no SOAP).
- Tamanho: bytes do corpo da resposta HTTP.

## Tempo de resposta

| Serviço | OK/total | Média (ms) | Mediana | Mín | Máx | Desvio | Só rede (média) | Tamanho (bytes) | Nova tentativa | Falhas |
|---|---|---|---|---|---|---|---|---|---|---|
| REST detalhe | 10/10 | 324.7 | 318.1 | 283.9 | 435.9 | 41.8 | - | 1.661 | 0 | 0 |
| SOAP estabelecimento | 10/10 | 134.8 | 133.1 | 118.8 | 154.5 | 11.4 | 130.2 | 26.942 | 0 | 0 |
| SOAP leitos | 10/10 | 109.9 | 88.3 | 71.2 | 222.6 | 52.3 | 108.6 | 2.001 | 0 | 0 |

## Carregamento dos WSDLs (custo fixo do SOAP, uma vez, sem cache)

| WSDL | Carregamento (ms) |
|---|---|
| CnesService (estabelecimento) | 3.563 |
| LeitoService (leitos) | 516 |

## Conteúdo das respostas

Bytes contados no XML compacto (sem indentação). "% marcação" = parte da resposta que não é valor de dado (chaves e aspas no JSON; tags e namespaces no XML).

| Resposta | Bytes | Itens (campos/elementos) | Valores preenchidos | Bytes de dado | % marcação | Cabeçalho (bytes) | Corpo (bytes) | Declarações xmlns |
|---|---|---|---|---|---|---|---|---|
| REST detalhe (JSON) | 1.661 | 37 campos | 31 | 314 | 81.1% | - | - | - |
| SOAP estabelecimento (XML) | 26.946 | 85 elementos | 53 | 644 | 97.6% | 355 | 26.455 | 287 |
| SOAP leitos (XML) | 2.001 | 11 elementos | 6 | 144 | 92.8% | 355 | 1.510 | 22 |

### O que vem no corpo do SOAP estabelecimento

| Seção do corpo (SOAP estabelecimento) | Ocorrências | Valores de dado | Declarações xmlns | Bytes | % do corpo |
|---|---|---|---|---|---|
| EstabelecimentoSaude | 1 | 26 | 56 | 6.329 | 23.9% |
| profissional | 1 | 8 | 56 | 5.078 | 19.2% |
| equipamento | 1 | 7 | 56 | 4.942 | 18.7% |
| sumario | 1 | 6 | 56 | 4.896 | 18.5% |
| leito | 1 | 5 | 56 | 4.803 | 18.2% |
