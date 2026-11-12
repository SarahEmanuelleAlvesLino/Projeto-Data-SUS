# benchmark.py
# Compara o desempenho e o conteúdo das respostas REST x SOAP para o mesmo
# estabelecimento.
#
# Uso:
#   python3 benchmark.py                      (CNES 4884124, 10 chamadas)
#   python3 benchmark.py --cnes 2077620 -n 5 --pausa 1
#
# Gera:
#   resultados/desempenho.md            tabelas em Markdown (para o README)
#   resultados/desempenho.csv           resumo por serviço (para gráficos)
#   resultados/desempenho_chamadas.csv  cada chamada individual
# --------------------------------------------------------------------------

import argparse
import csv
import json
import statistics
import time
from datetime import datetime
from pathlib import Path

from lxml import etree

import rest_client
import soap_client

PASTA_RESULTADOS = Path(__file__).resolve().parent / "resultados"


# Medição de desempenho -----------------------------------------------------

def chamar(nome, funcao, cnes):
    """Executa uma chamada e devolve (registro da medição, resposta ou None)."""
    try:
        resposta = funcao(cnes)
        return {
            "servico": nome, "sucesso": True,
            "tempo_ms": resposta.tempo_ms,
            "tempo_http_ms": getattr(resposta, "tempo_http_ms", None),
            "tamanho_bytes": resposta.tamanho_bytes,
            "tentativas": getattr(resposta, "tentativas", 1),   # o REST não tem novas tentativas
            "erro": "",
        }, resposta
    except (rest_client.ErroAPIREST, soap_client.ErroAPISOAP) as erro:
        return {
            "servico": nome, "sucesso": False, "tempo_ms": None, "tempo_http_ms": None,
            "tamanho_bytes": None, "tentativas": getattr(erro, "tentativas", 1),
            "erro": str(erro),
        }, None


def estatisticas(valores):
    """Média, mediana, mínimo, máximo e desvio padrão de uma lista de números."""
    if not valores:
        return None
    return {
        "media": statistics.mean(valores),
        "mediana": statistics.median(valores),
        "min": min(valores),
        "max": max(valores),
        "desvio": statistics.stdev(valores) if len(valores) > 1 else 0.0,
    }


def resumir(nome, registros):
    sucesso = [r for r in registros if r["sucesso"]]
    rede = [r["tempo_http_ms"] for r in sucesso if r["tempo_http_ms"] is not None]
    return {
        "servico": nome,
        "chamadas": len(registros),
        "ok": len(sucesso),
        "falhas": len(registros) - len(sucesso),
        "com_nova_tentativa": sum(1 for r in registros if r["tentativas"] > 1),
        "tempo": estatisticas([r["tempo_ms"] for r in sucesso]),
        "rede_media": statistics.mean(rede) if rede else None,
        "tamanho_bytes": sucesso[-1]["tamanho_bytes"] if sucesso else None,
        "ultimo_erro": next((r["erro"] for r in reversed(registros) if r["erro"]), ""),
    }


# Análise de conteúdo -------------------------------------------------------

def analisar_json(texto):
    """Conta os valores do JSON e separa bytes de dado x bytes de marcação."""
    folhas = []

    def percorrer(v):
        if isinstance(v, dict):
            for x in v.values():
                percorrer(x)
        elif isinstance(v, list):
            for x in v:
                percorrer(x)
        else:
            folhas.append(v)

    percorrer(json.loads(texto))
    preenchidos = [f for f in folhas if f not in (None, "")]
    total = len(texto.encode("utf-8"))
    dado = sum(len(str(f).encode("utf-8")) for f in preenchidos)
    return {
        "campos": len(folhas),
        "valores_preenchidos": len(preenchidos),
        "bytes_total": total,
        "bytes_dado": dado,
        "pct_marcacao": 100 * (total - dado) / total,
    }


def _bytes(elemento):
    return len(etree.tostring(elemento))   # serialização compacta (sem indentação)


def _ns_proprios(elemento):
    """Namespaces que o próprio elemento declara (não herdados do pai)."""
    pai = elemento.getparent()
    herdados = pai.nsmap if pai is not None else {}
    return {p: u for p, u in elemento.nsmap.items() if herdados.get(p) != u}


def _bytes_no_documento(elemento):
    """Bytes que o elemento ocupa dentro do documento.

    Ao serializar um elemento isolado, o lxml repete nele os namespaces
    herdados dos ancestrais. Descontamos essas declarações extras para
    contar só o que realmente está no XML recebido.
    """
    proprios = _ns_proprios(elemento)
    extras = sum(
        len(f' xmlns:{p}="{u}"' if p else f' xmlns="{u}"')
        for p, u in elemento.nsmap.items() if p not in proprios
    )
    return _bytes(elemento) - extras


def analisar_xml(texto):
    """Conta os elementos do XML, separa cabeçalho x corpo e dado x marcação."""
    raiz = etree.fromstring(texto.encode("utf-8"))
    # Remove a indentação do pretty print, para medir o XML compacto
    for el in raiz.iter():
        if el.text is not None and not el.text.strip() and len(el):
            el.text = None
        if el.tail is not None and not el.tail.strip():
            el.tail = None

    elementos = [el for el in raiz.iter() if isinstance(el.tag, str)]
    folhas = [el for el in elementos if len(el) == 0]
    preenchidas = [el for el in folhas if (el.text or "").strip()]

    # Declarações de namespace (xmlns:...) feitas em cada elemento
    declaracoes = sum(len(_ns_proprios(el)) for el in elementos)

    total = _bytes(raiz)
    dado = sum(len(el.text.strip().encode("utf-8")) for el in preenchidas)
    header = next((el for el in raiz if etree.QName(el).localname == "Header"), None)
    body = next((el for el in raiz if etree.QName(el).localname == "Body"), None)

    # Seções do corpo: filhos do elemento de resultado (ex.: EstabelecimentoSaude,
    # profissional, leito...), agrupados pelo nome
    secoes = {}
    if body is not None and len(body) and len(body[0]):
        resultado = body[0][0] if len(body[0][0]) else body[0]
        for filho in resultado:
            if isinstance(filho.tag, str):
                nome = etree.QName(filho).localname
                valores = sum(1 for e in filho.iter() if len(e) == 0 and (e.text or "").strip())
                qtd, tam, val, ns = secoes.get(nome, (0, 0, 0, 0))
                secoes[nome] = (qtd + 1, tam + _bytes_no_documento(filho),
                                val + valores, ns + len(_ns_proprios(filho)))

    return {
        "elementos": len(elementos),
        "valores_preenchidos": len(preenchidas),
        "declaracoes_namespace": declaracoes,
        "bytes_total": total,
        "bytes_header": _bytes_no_documento(header) if header is not None else 0,
        "bytes_body": _bytes_no_documento(body) if body is not None else 0,
        "bytes_dado": dado,
        "pct_marcacao": 100 * (total - dado) / total,
        "secoes": secoes,
    }


# Formatação ----------------------------------------------------------------

def fmt(valor, casas=0, sufixo=""):
    if valor is None:
        return "-"
    return f"{valor:,.{casas}f}{sufixo}".replace(",", ".")


def tabela_markdown(cabecalho, linhas):
    saida = ["| " + " | ".join(cabecalho) + " |",
             "|" + "|".join("---" for _ in cabecalho) + "|"]
    saida += ["| " + " | ".join(str(c) for c in linha) + " |" for linha in linhas]
    return "\n".join(saida)


def tabela_terminal(cabecalho, linhas):
    larguras = [max(len(str(x)) for x in coluna) for coluna in zip(cabecalho, *linhas)]
    def linha(valores):
        return "  ".join(str(v).ljust(w) if i == 0 else str(v).rjust(w)
                         for i, (v, w) in enumerate(zip(valores, larguras)))
    return "\n".join([linha(cabecalho), "  ".join("-" * w for w in larguras)]
                     + [linha(l) for l in linhas])


# Programa principal ----------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Benchmark REST x SOAP do CNES")
    parser.add_argument("--cnes", default="4884124", help="código CNES (padrão 4884124)")
    parser.add_argument("-n", type=int, default=10, help="chamadas por serviço (padrão 10)")
    parser.add_argument("--pausa", type=float, default=0.5,
                        help="pausa entre chamadas, em segundos (padrão 0,5)")
    args = parser.parse_args()
    inicio_execucao = datetime.now()

    servicos = [
        ("REST detalhe", rest_client.buscar_estabelecimento),
        ("SOAP estabelecimento", lambda c: soap_client.consultar_estabelecimento(c, usar_cache=False)),
        ("SOAP leitos", lambda c: soap_client.consultar_leitos(c, usar_cache=False)),
    ]

    # 1) Carregamento dos WSDLs, SEM o cache em disco (custo real pela rede)
    print("== Carregando os WSDLs (sem cache) ==")
    wsdl = {}
    for nome in ("estabelecimento", "leitos"):
        try:
            wsdl[nome] = soap_client.medir_carregamento_wsdl(nome, usar_cache_wsdl=False)
            print(f"   {nome:<16} {wsdl[nome]:8.0f} ms")
        except soap_client.ErroAPISOAP as erro:
            wsdl[nome] = None
            print(f"   {nome:<16} FALHOU: {erro}")

    # 2) Aquecimento: uma chamada de cada, fora da conta (carrega os clientes)
    print("\n== Aquecimento (1 chamada de cada, não entra na conta) ==")
    for nome, funcao in servicos:
        registro, _ = chamar(nome, funcao, args.cnes)
        print(f"   {nome:<22} {'ok' if registro['sucesso'] else 'falhou: ' + registro['erro'][:70]}")

    # 3) N rodadas intercaladas: REST, SOAP estabelecimento, SOAP leitos
    print(f"\n== {args.n} rodadas, CNES {args.cnes}, pausa de {args.pausa} s ==")
    registros = {nome: [] for nome, _ in servicos}
    ultimas = {}
    for rodada in range(1, args.n + 1):
        partes = []
        for nome, funcao in servicos:
            registro, resposta = chamar(nome, funcao, args.cnes)
            registro["rodada"] = rodada
            registros[nome].append(registro)
            if resposta is not None:
                ultimas[nome] = resposta
            partes.append(f"{nome.split()[0]} {fmt(registro['tempo_ms'])} ms"
                          if registro["sucesso"] else f"{nome.split()[0]} FALHOU")
            time.sleep(args.pausa)
        print(f"   rodada {rodada:>2}: " + " | ".join(partes))

    resumos = [resumir(nome, registros[nome]) for nome, _ in servicos]

    # Tabela de desempenho
    cab_desempenho = ["Serviço", "OK/total", "Média (ms)", "Mediana", "Mín", "Máx",
                      "Desvio", "Só rede (média)", "Tamanho (bytes)", "Nova tentativa", "Falhas"]
    lin_desempenho = []
    for r in resumos:
        t = r["tempo"] or {}
        lin_desempenho.append([
            r["servico"], f"{r['ok']}/{r['chamadas']}",
            fmt(t.get("media"), 1), fmt(t.get("mediana"), 1), fmt(t.get("min"), 1),
            fmt(t.get("max"), 1), fmt(t.get("desvio"), 1), fmt(r["rede_media"], 1),
            fmt(r["tamanho_bytes"]), r["com_nova_tentativa"], r["falhas"],
        ])
    cab_wsdl = ["WSDL", "Carregamento (ms)"]
    lin_wsdl = [["CnesService (estabelecimento)", fmt(wsdl["estabelecimento"]) if wsdl["estabelecimento"] is not None else "falhou"],
                ["LeitoService (leitos)", fmt(wsdl["leitos"]) if wsdl["leitos"] is not None else "falhou"]]

    # Comparação de conteúdo (última resposta bem-sucedida de cada)
    cab_conteudo = ["Resposta", "Bytes", "Itens (campos/elementos)", "Valores preenchidos",
                    "Bytes de dado", "% marcação", "Cabeçalho (bytes)", "Corpo (bytes)",
                    "Declarações xmlns"]
    lin_conteudo, analises_xml = [], {}
    if "REST detalhe" in ultimas:
        a = analisar_json(ultimas["REST detalhe"].json_bruto)
        lin_conteudo.append(["REST detalhe (JSON)", fmt(a["bytes_total"]), f"{a['campos']} campos",
                             a["valores_preenchidos"], fmt(a["bytes_dado"]),
                             fmt(a["pct_marcacao"], 1, "%"), "-", "-", "-"])
    for nome in ("SOAP estabelecimento", "SOAP leitos"):
        if nome in ultimas:
            a = analisar_xml(ultimas[nome].xml_bruto)
            analises_xml[nome] = a
            lin_conteudo.append([f"{nome} (XML)", fmt(a["bytes_total"]), f"{a['elementos']} elementos",
                                 a["valores_preenchidos"], fmt(a["bytes_dado"]),
                                 fmt(a["pct_marcacao"], 1, "%"), fmt(a["bytes_header"]),
                                 fmt(a["bytes_body"]), a["declaracoes_namespace"]])

    cab_secoes = ["Seção do corpo (SOAP estabelecimento)", "Ocorrências", "Valores de dado",
                  "Declarações xmlns", "Bytes", "% do corpo"]
    lin_secoes = []
    if "SOAP estabelecimento" in analises_xml:
        a = analises_xml["SOAP estabelecimento"]
        for nome, (qtd, tam, val, ns) in sorted(a["secoes"].items(), key=lambda x: -x[1][1]):
            lin_secoes.append([nome, qtd, val, ns, fmt(tam),
                               fmt(100 * tam / a["bytes_body"], 1, "%")])

    # Saída no terminal
    print("\n== Desempenho (tempo da chamada, sem carregar o WSDL) ==")
    print(tabela_terminal(cab_desempenho, lin_desempenho))
    print("\n== Carregamento dos WSDLs (uma vez, sem cache) ==")
    print(tabela_terminal(cab_wsdl, lin_wsdl))
    print("\n== Conteúdo das respostas ==")
    print(tabela_terminal(cab_conteudo, lin_conteudo))
    if lin_secoes:
        print()
        print(tabela_terminal(cab_secoes, lin_secoes))
    for r in resumos:
        if r["falhas"]:
            print(f"\n   Última falha em {r['servico']}: {r['ultimo_erro']}")

    # Arquivos
    PASTA_RESULTADOS.mkdir(exist_ok=True)
    data_hora = inicio_execucao.strftime("%d/%m/%Y %H:%M")
    md = [
        "# Desempenho REST x SOAP",
        "",
        f"- Data/hora: {data_hora}",
        f"- Estabelecimento: CNES {soap_client.formatar_codigo_cnes(args.cnes)}",
        f"- Chamadas por serviço: {args.n} (intercaladas, pausa de {args.pausa} s, "
        "após 1 chamada de aquecimento)",
        "- Tempo: da chamada da função até ter os dados em Python (rede + processamento). "
        "\"Só rede\" = envio, espera e download do HTTP (só medido no SOAP).",
        "- Tamanho: bytes do corpo da resposta HTTP.",
        "",
        "## Tempo de resposta",
        "",
        tabela_markdown(cab_desempenho, lin_desempenho),
        "",
        "## Carregamento dos WSDLs (custo fixo do SOAP, uma vez, sem cache)",
        "",
        tabela_markdown(cab_wsdl, lin_wsdl),
        "",
        "## Conteúdo das respostas",
        "",
        "Bytes contados no XML compacto (sem indentação). \"% marcação\" = parte da "
        "resposta que não é valor de dado (chaves e aspas no JSON; tags e namespaces no XML).",
        "",
        tabela_markdown(cab_conteudo, lin_conteudo),
        "",
    ]
    if lin_secoes:
        md += ["### O que vem no corpo do SOAP estabelecimento", "",
               tabela_markdown(cab_secoes, lin_secoes), ""]
    (PASTA_RESULTADOS / "desempenho.md").write_text("\n".join(md), encoding="utf-8")

    with open(PASTA_RESULTADOS / "desempenho.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["data_hora", "cnes", "servico", "chamadas", "ok", "falhas",
                    "com_nova_tentativa", "media_ms", "mediana_ms", "min_ms", "max_ms",
                    "desvio_ms", "rede_media_ms", "tamanho_bytes"])
        for r in resumos:
            t = r["tempo"] or {}
            w.writerow([inicio_execucao.isoformat(timespec="seconds"), args.cnes, r["servico"],
                        r["chamadas"], r["ok"], r["falhas"], r["com_nova_tentativa"]]
                       + [round(t[k], 1) if k in t else "" for k in
                          ("media", "mediana", "min", "max", "desvio")]
                       + [round(r["rede_media"], 1) if r["rede_media"] is not None else "",
                          r["tamanho_bytes"] or ""])
        for nome, ms in (("WSDL CnesService", wsdl["estabelecimento"]),
                         ("WSDL LeitoService", wsdl["leitos"])):
            w.writerow([inicio_execucao.isoformat(timespec="seconds"), args.cnes, nome,
                        1, int(ms is not None), int(ms is None), 0,
                        round(ms, 1) if ms is not None else "", "", "", "", "", "", ""])

    with open(PASTA_RESULTADOS / "desempenho_chamadas.csv", "w", newline="", encoding="utf-8") as f:
        campos = ["rodada", "servico", "sucesso", "tempo_ms", "tempo_http_ms",
                  "tamanho_bytes", "tentativas", "erro"]
        w = csv.DictWriter(f, fieldnames=campos)
        w.writeheader()
        for nome, _ in servicos:
            for r in registros[nome]:
                w.writerow({k: (round(r[k], 1) if isinstance(r[k], float) else r[k]) for k in campos})

    print(f"\nArquivos salvos em {PASTA_RESULTADOS}/: desempenho.md, desempenho.csv, "
          "desempenho_chamadas.csv")


if __name__ == "__main__":
    main()
