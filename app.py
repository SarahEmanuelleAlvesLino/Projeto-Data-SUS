# app.py
# Interface Streamlit do "Monitor de Estabelecimentos de Saúde (CNES)".
#
# Fluxo: município -> lista via REST (JSON) -> escolha de um estabelecimento
#        -> detalhe e leitos via SOAP (XML) -> comparação dos dois protocolos.
#
# Rodar:  streamlit run app.py
# Simular o SOAP fora do ar:  MONITOR_SIMULAR_SOAP_FORA=1 streamlit run app.py
#         (ou ligar "Simular SOAP fora do ar" na barra lateral)
# --------------------------------------------------------------------------

import json
import math
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import altair as alt
import pandas as pd
import requests
import streamlit as st
from lxml import etree

import rest_client
import soap_client

# Configuração ---------------------------------------------------------------

st.set_page_config(page_title="Monitor CNES", page_icon="🏥", layout="wide")

PASTA = Path(__file__).resolve().parent
CSV_DEPOIS = PASTA / "resultados" / "desempenho.csv"                  # REST com Session
CSV_ANTES = PASTA / "resultados" / "antes_session" / "desempenho.csv"  # REST sem Session

# Capitais com o código IBGE de 7 dígitos (o rest_client converte para 6)
CAPITAIS = {
    "São Paulo (SP)": 3550308,
    "Rio de Janeiro (RJ)": 3304557,
    "Belo Horizonte (MG)": 3106200,
    "Salvador (BA)": 2927408,
    "Brasília (DF)": 5300108,
    "Curitiba (PR)": 4106902,
    "Recife (PE)": 2611606,
    "Porto Alegre (RS)": 4314902,
    "Fortaleza (CE)": 2304400,
    "Manaus (AM)": 1302603,
}
OUTRO_MUNICIPIO = "Outro código IBGE"
TODOS_TIPOS = "Todos"
TIPO_PADRAO = 5   # HOSPITAL GERAL

# Cores fixas por protocolo em todos os gráficos (REST = azul, SOAP = laranja)
COR_REST = "#2a78d6"
COR_REST_ANTES = "#86b6ef"    # mesmo azul, mais claro: "REST sem Session"
COR_SOAP = "#eb6834"
COR_TOTAL = "#6250d6"
COR_SUS = "#1baf7a"

LIMITE_XML_NA_TELA = 150_000  # caracteres; XML maior é cortado na tela (download vem inteiro)


# Formatação -----------------------------------------------------------------

def fmt_ms(valor):
    return "—" if valor is None else f"{valor:,.0f} ms".replace(",", ".")


def fmt_bytes(valor):
    if valor is None:
        return "—"
    if valor < 1024:
        return f"{valor} B"
    if valor < 1024 ** 2:
        return f"{valor / 1024:.1f} KB".replace(".", ",")
    return f"{valor / 1024 ** 2:.1f} MB".replace(".", ",")


def fmt_data(iso):
    try:
        return datetime.fromisoformat(iso).strftime("%d/%m/%Y às %H:%M")
    except (TypeError, ValueError):
        return "data desconhecida"


# Dados com cache do Streamlit -------------------------------------------------

@st.cache_data(ttl=3600, show_spinner=False)
def carregar_tipos_unidade():
    """Tipos de unidade do REST (/cnes/tipounidades). Guardado por 1 hora."""
    resposta = rest_client.listar_tipos_unidade()
    return sorted(
        ((t["codigo_tipo_unidade"], t["descricao_tipo_unidade"]) for t in resposta.dados),
        key=lambda t: t[1],
    )


@st.cache_data(ttl=60, show_spinner=False)
def verificar_servicos(simulando_falha: bool):
    """Checagem leve de disponibilidade (refeita no máximo a cada 60 s).

    REST: GET em /cnes/tipounidades.  SOAP: GET no WSDL do LeitoService
    (mostra se o barramento do DATASUS está respondendo).
    """
    def checar(url, conferir):
        inicio = time.perf_counter()
        try:
            r = requests.get(url, timeout=5)
            ok = r.status_code == 200 and conferir(r)
            return {"ok": ok, "ms": (time.perf_counter() - inicio) * 1000,
                    "detalhe": f"HTTP {r.status_code}"}
        except requests.exceptions.RequestException as erro:
            return {"ok": False, "ms": None, "detalhe": type(erro).__name__}

    rest = checar(rest_client.BASE_URL + "/cnes/tipounidades", lambda r: "tipos_unidade" in r.text)
    if simulando_falha:
        soap = {"ok": False, "ms": None, "detalhe": "falha simulada"}
    else:
        soap = checar(soap_client.WSDL_LEITOS, lambda r: b"definitions" in r.content)
    return {"rest": rest, "soap": soap, "verificado_em": datetime.now().strftime("%H:%M:%S")}


@st.cache_resource(show_spinner=False)
def preparar_soap():
    """Carrega os WSDLs UMA vez por servidor (st.cache_resource).

    Se der erro, a exceção não fica guardada: tenta de novo no próximo acesso.
    """
    do_disco = (soap_client.PASTA_CACHE / "wsdl.sqlite").exists()
    inicio = time.perf_counter()
    tempos = soap_client.carregar_clientes()
    return {"tempos": tempos, "total_ms": (time.perf_counter() - inicio) * 1000,
            "do_disco": do_disco}


@st.cache_data(show_spinner=False)
def ler_benchmark(caminho: str, modificado_em: float):
    """Lê um CSV do benchmark (modificado_em invalida o cache se o arquivo mudar)."""
    return pd.read_csv(caminho)


# Estado da sessão -------------------------------------------------------------

def iniciar_estado():
    padrao = {
        "busca": None,        # parâmetros da última busca REST
        "offset": 0,          # paginação (quantos itens pular)
        "lista": None,        # RespostaREST da listagem
        "erro_lista": None,
        "detalhe": None,      # resultado da consulta de um estabelecimento
    }
    for chave, valor in padrao.items():
        st.session_state.setdefault(chave, valor)


# Ações ---------------------------------------------------------------------------

def executar_busca():
    """Chama o REST com os parâmetros guardados e o offset atual."""
    busca = st.session_state.busca
    try:
        st.session_state.lista = rest_client.listar_estabelecimentos(
            codigo_municipio=busca["municipio"],
            codigo_tipo_unidade=busca["tipo"],
            status=1 if busca["somente_ativos"] else None,
            limit=busca["limit"],
            offset=st.session_state.offset,
        )
        st.session_state.erro_lista = None
    except rest_client.ErroAPIREST as erro:
        st.session_state.lista = None
        st.session_state.erro_lista = str(erro)


def nova_busca(parametros):
    st.session_state.busca = parametros
    st.session_state.offset = 0
    st.session_state.detalhe = None
    executar_busca()


def mudar_pagina(direcao: int):
    # Na API, offset = número da PÁGINA (começa em 0), não itens a pular.
    st.session_state.offset = max(0, st.session_state.offset + direcao)
    executar_busca()


def consultar_detalhes(cnes):
    """Consulta o mesmo estabelecimento no REST e nos dois serviços SOAP.

    Cada chamada é independente: se o SOAP falhar, o REST continua
    aparecendo (e vice-versa).
    """
    det = {"cnes": cnes, "rest": None, "erro_rest": None,
           "est": None, "erro_est": None, "leitos": None, "erro_leitos": None,
           "sem_leitos": False, "xml_est": None, "xml_leitos": None}

    try:
        det["rest"] = rest_client.buscar_estabelecimento(cnes)
    except rest_client.ErroAPIREST as erro:
        det["erro_rest"] = str(erro)

    try:
        det["est"] = soap_client.consultar_estabelecimento(cnes)
        det["xml_est"] = soap_client.mascarar_dados_pessoais(det["est"].xml_bruto)
    except soap_client.ErroAPISOAP as erro:
        det["erro_est"] = str(erro)

    try:
        det["leitos"] = soap_client.consultar_leitos(cnes)
        det["xml_leitos"] = soap_client.mascarar_dados_pessoais(det["leitos"].xml_bruto)
    except soap_client.ErroAPISOAP as erro:
        if erro.codigo_falha == "NO_RESULT":
            det["sem_leitos"] = True          # não é erro: só não há leitos cadastrados
        else:
            det["erro_leitos"] = str(erro)

    st.session_state.detalhe = det


# Barra lateral ---------------------------------------------------------------

def desenhar_barra_lateral():
    """Desenha os filtros e devolve os parâmetros escolhidos."""
    st.sidebar.header("🔎 Filtros")

    nome = st.sidebar.selectbox("Município", list(CAPITAIS) + [OUTRO_MUNICIPIO], key="municipio")
    if nome == OUTRO_MUNICIPIO:
        codigo = st.sidebar.text_input("Código IBGE (6 ou 7 dígitos)", value="2304400",
                                       key="ibge_livre").strip()
    else:
        codigo = CAPITAIS[nome]
        st.sidebar.caption(f"Código IBGE: {codigo} (a API usa os 6 primeiros: {str(codigo)[:6]})")

    try:
        tipos = carregar_tipos_unidade()
    except rest_client.ErroAPIREST as erro:
        st.sidebar.warning(f"Não foi possível carregar os tipos de unidade: {erro}")
        tipos = [(TIPO_PADRAO, "HOSPITAL GERAL")]
    rotulos = [TODOS_TIPOS] + [f"{desc.title()} ({cod})" for cod, desc in tipos]
    codigos = [None] + [cod for cod, _ in tipos]
    indice_padrao = codigos.index(TIPO_PADRAO) if TIPO_PADRAO in codigos else 0
    escolha = st.sidebar.selectbox("Tipo de unidade", rotulos, index=indice_padrao, key="tipo")
    tipo = codigos[rotulos.index(escolha)]

    somente_ativos = st.sidebar.checkbox("Somente estabelecimentos ativos", value=True,
                                         key="ativos")
    limit = st.sidebar.slider("Quantidade por página", 5, rest_client.LIMITE_MAXIMO, 10,
                              key="limit")

    desenhar_status_servicos()

    with st.sidebar.expander("🧪 Modo demonstração"):
        soap_client.simular_falha = st.toggle(
            "Simular SOAP fora do ar", value=soap_client.simular_falha, key="simular",
            help="Toda chamada SOAP devolve um 504 simulado. Mostra as novas "
                 "tentativas e o uso da última resposta salva em cache/.",
        )

    return {"municipio": codigo, "municipio_nome": nome, "tipo": tipo, "tipo_nome": escolha,
            "somente_ativos": somente_ativos, "limit": limit}


def desenhar_status_servicos():
    st.sidebar.divider()
    st.sidebar.subheader("Status dos serviços")
    status = verificar_servicos(soap_client.simular_falha)
    for rotulo, chave in (("REST (Dados Abertos)", "rest"), ("SOAP (barramento CNES)", "soap")):
        s = status[chave]
        if s["ok"]:
            st.sidebar.markdown(f"🟢 **{rotulo}** — respondendo ({fmt_ms(s['ms'])})")
        else:
            st.sidebar.markdown(f"🔴 **{rotulo}** — indisponível ({s['detalhe']})")
    st.sidebar.caption(f"Verificado às {status['verificado_em']} (atualiza a cada 60 s)")


# Lista e mapa (REST) -------------------------------------------------------------

def tabela_estabelecimentos(lista):
    linhas = []
    for e in lista:
        endereco = ", ".join(p for p in (e.get("endereco_estabelecimento"),
                                         e.get("numero_estabelecimento")) if p)
        sus = e.get("estabelecimento_faz_atendimento_ambulatorial_sus")
        linhas.append({
            "CNES": str(e["codigo_cnes"]),
            "Nome fantasia": e.get("nome_fantasia"),
            "Endereço": endereco,
            "Bairro": e.get("bairro_estabelecimento"),
            "Atende SUS (ambulatorial)": {"SIM": "Sim", "NAO": "Não"}.get(sus, "—"),
        })
    return pd.DataFrame(linhas)


def desenhar_lista():
    resposta = st.session_state.lista
    busca = st.session_state.busca

    st.subheader(f"Estabelecimentos — {busca['municipio_nome']} · {busca['tipo_nome']}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Tempo (REST)", fmt_ms(resposta.tempo_ms))
    c2.metric("Tamanho da resposta", fmt_bytes(resposta.tamanho_bytes))
    c3.metric("HTTP", resposta.status_code)
    c4.metric("Itens nesta página", len(resposta.dados))
    st.caption(f"`GET {resposta.url}`")

    if not resposta.dados:
        st.info("Nenhum estabelecimento encontrado com esses filtros."
                + (" Volte para a página anterior." if st.session_state.offset else ""))
    else:
        st.dataframe(tabela_estabelecimentos(resposta.dados), hide_index=True,
                     width="stretch")

    pagina = st.session_state.offset + 1
    p1, p2, p3 = st.columns([1, 2, 1])
    p1.button("◀ Anterior", on_click=mudar_pagina, args=(-1,),
              disabled=st.session_state.offset == 0, width="stretch")
    p2.markdown(f"<div style='text-align:center;padding-top:.4rem'>Página {pagina} "
                f"(offset = {st.session_state.offset})</div>", unsafe_allow_html=True)
    p3.button("Próxima ▶", on_click=mudar_pagina, args=(1,),
              disabled=len(resposta.dados) < busca["limit"], width="stretch")


def desenhar_mapa():
    pontos = [
        {"lat": e.get("latitude_estabelecimento_decimo_grau"),
         "lon": e.get("longitude_estabelecimento_decimo_grau")}
        for e in st.session_state.lista.dados
    ]
    pontos = [p for p in pontos if p["lat"] and p["lon"]]
    total = len(st.session_state.lista.dados)
    if not pontos:
        st.caption("Nenhum estabelecimento desta página tem latitude/longitude no REST.")
        return
    with st.expander(f"🗺️ Mapa ({len(pontos)} de {total} com coordenadas)", expanded=True):
        st.map(pd.DataFrame(pontos), latitude="lat", longitude="lon", size=80,
               color=COR_REST, height=360)


def desenhar_selecao():
    lista = st.session_state.lista.dados
    if not lista:
        return
    st.subheader("Detalhar um estabelecimento")
    opcoes = {f"{e['codigo_cnes']} — {e.get('nome_fantasia')}": e["codigo_cnes"] for e in lista}
    c1, c2 = st.columns([3, 1])
    escolha = c1.selectbox("Estabelecimento", list(opcoes), key="escolha",
                           label_visibility="collapsed")
    if c2.button("Consultar detalhes (SOAP)", type="primary", width="stretch"):
        with st.spinner("Consultando o REST e os serviços SOAP (hospitais grandes podem "
                        "levar alguns segundos: a resposta traz todos os profissionais)..."):
            consultar_detalhes(opcoes[escolha])


# Abas do detalhe -----------------------------------------------------------------

def avisos_soap(det):
    """Avisos amigáveis de falha / cache do SOAP (nunca um traceback)."""
    for resposta in (det["est"], det["leitos"]):
        if resposta is not None and resposta.do_cache:
            st.info(f"⚠️ Serviço SOAP indisponível — exibindo última resposta salva em "
                    f"{fmt_data(resposta.salvo_em)} (após {resposta.tentativas} tentativas).")
            break
    if det["erro_est"]:
        st.warning(f"Serviço SOAP (estabelecimento): {det['erro_est']}")
    if det["erro_rest"]:
        st.warning(f"API REST: {det['erro_rest']}")


def aba_visao_geral(det, tipos):
    rest = det["rest"].dados if det["rest"] else {}
    soap = soap_client.resumir_estabelecimento(det["est"].dados) if det["est"] else {}

    def r(campo):
        valor = rest.get(campo)
        return "—" if valor in (None, "") else str(valor)

    endereco_rest = ", ".join(p for p in (rest.get("endereco_estabelecimento"),
                                          rest.get("numero_estabelecimento")) if p)
    linhas = [
        ("Nome fantasia", r("nome_fantasia"), soap.get("nome_fantasia")),
        ("Razão social", r("nome_razao_social"), soap.get("razao_social")),
        ("CNPJ", r("numero_cnpj"), soap.get("cnpj")),
        ("Tipo de unidade", tipos.get(rest.get("codigo_tipo_unidade"), r("codigo_tipo_unidade")),
         soap.get("tipo_unidade")),
        ("Esfera administrativa", r("descricao_esfera_administrativa"),
         soap.get("esfera_administrativa")),
        ("Endereço", endereco_rest or "—", soap.get("endereco")),
        ("Bairro", r("bairro_estabelecimento"), None),
        ("CEP", r("codigo_cep_estabelecimento"), soap.get("cep")),
        ("Município / UF", r("codigo_municipio"),
         f"{soap.get('municipio')}/{soap.get('uf')}" if soap.get("municipio") else None),
        ("Telefone", r("numero_telefone_estabelecimento"), ", ".join(soap.get("telefones") or [])),
        ("E-mail", r("endereco_email_estabelecimento"), soap.get("email")),
        ("Atualizado em", r("data_atualizacao"), soap.get("data_atualizacao")),
    ]
    tabela = pd.DataFrame(
        [{"Campo": c, "REST (JSON)": v_rest, "SOAP (XML)": v_soap or "—"} for c, v_rest, v_soap in linhas]
    )
    st.dataframe(tabela, hide_index=True, width="stretch")
    st.caption("O REST não traz o nome do município nem a descrição do tipo de unidade "
               "(só códigos); o SOAP já traz os nomes, além do bairro dentro do endereço.")

    if det["est"]:
        sumario = soap.get("sumario") or {}
        st.markdown("**Sumário oficial (só no SOAP)**")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Profissionais", sumario.get("quantidadeProfissionaisSaude", "—"))
        c2.metric("Tipos de leito", sumario.get("quantidadeLeitos", "—"))
        c3.metric("Equipamentos", sumario.get("quantidadeEquipamentos", "—"))
        c4.metric("Habilitações", sumario.get("quantidadeHabilitacoes", "—"))
        extras = (det["est"].dados.get("EstabelecimentoSaude") or {}).get("_raw_elements") or []
        for extra in extras:
            for nome, valor in extra.items():
                st.caption(f"Campo fora do WSDL recebido pelo SOAP — **{nome}**: {valor} "
                           "(é por causa dele que o cliente usa `strict=False`).")


def aba_leitos(det):
    if det["erro_leitos"]:
        st.warning(f"Serviço SOAP (leitos): {det['erro_leitos']}")
        return
    # O LeitoService indica "sem leitos" de dois jeitos: SOAP Fault NO_RESULT
    # (det["sem_leitos"]) ou HTTP 200 com a resposta vazia (lista vazia).
    leitos = (det["leitos"].dados or []) if det["leitos"] else []
    if det["sem_leitos"] or not leitos:
        st.info("Este estabelecimento não tem leitos cadastrados no CNES — comum em clínicas, "
                "consultórios e unidades básicas, que não fazem internação.")
        return
    total = sum(l["quantidadeLeito"] or 0 for l in leitos)
    sus = sum(l["quantidadeLeitoSUS"] or 0 for l in leitos)
    c1, c2, c3 = st.columns(3)
    c1.metric("Leitos (total)", total)
    c2.metric("Leitos SUS", sus)
    c3.metric("% SUS", f"{100 * sus / total:.0f}%" if total else "—")

    df = pd.DataFrame([{"Descrição": l["descricao"], "Total": l["quantidadeLeito"],
                        "SUS": l["quantidadeLeitoSUS"]} for l in leitos])
    df = df.sort_values("Total", ascending=False)
    st.dataframe(df, hide_index=True, width="stretch")

    longo = df.melt(id_vars="Descrição", var_name="Tipo", value_name="Leitos")
    grafico = (
        alt.Chart(longo, title="Leitos por tipo: total x SUS")
        .mark_bar(cornerRadiusEnd=4, height={"band": 0.8})
        .encode(
            y=alt.Y("Descrição:N", sort=list(df["Descrição"]), title=None),
            yOffset=alt.YOffset("Tipo:N", sort=["Total", "SUS"]),
            x=alt.X("Leitos:Q", title="Quantidade de leitos"),
            color=alt.Color("Tipo:N", sort=["Total", "SUS"], title=None,
                            scale=alt.Scale(domain=["Total", "SUS"], range=[COR_TOTAL, COR_SUS]),
                            legend=alt.Legend(orient="top")),
            tooltip=["Descrição", "Tipo", "Leitos"],
        )
        .properties(height=max(120, 46 * len(df)))
    )
    st.altair_chart(grafico, width="stretch")


def aba_profissionais(det):
    if not det["est"]:
        st.warning("Sem resposta do SOAP de estabelecimento para mostrar profissionais.")
        return
    dados = det["est"].dados
    profissionais = dados.get("profissional") or []
    sumario = dados.get("sumario") or {}

    # Conta quantos profissionais têm cada ocupação (CBO). Um profissional
    # pode ter mais de uma ocupação. Nenhum dado pessoal é lido aqui.
    contagem = Counter()
    for p in profissionais:
        ocupacoes = {c.get("descricaoCBO") for c in (p.get("CBO") or []) if c.get("descricaoCBO")}
        contagem.update(ocupacoes)

    c1, c2 = st.columns(2)
    c1.metric("Profissionais", sumario.get("quantidadeProfissionaisSaude", len(profissionais)))
    c2.metric("Ocupações diferentes (CBO)", len(contagem))
    st.caption("🔒 A resposta SOAP traz nome, CPF e CNS de cada profissional. Por privacidade "
               "(LGPD), a interface mostra apenas a quantidade e as ocupações.")
    if not contagem:
        st.info("Nenhuma ocupação informada para este estabelecimento.")
        return

    df = pd.DataFrame(contagem.most_common(), columns=["Ocupação (CBO)", "Profissionais"])
    topo = df.head(15)
    grafico = (
        alt.Chart(topo, title=f"Profissionais por ocupação ({'15 mais comuns' if len(df) > 15 else 'todas'})")
        .mark_bar(cornerRadiusEnd=4, color=COR_SOAP)
        .encode(
            y=alt.Y("Ocupação (CBO):N", sort="-x", title=None),
            x=alt.X("Profissionais:Q", title="Profissionais"),
            tooltip=["Ocupação (CBO)", "Profissionais"],
        )
        .properties(height=max(120, 28 * len(topo)))
    )
    st.altair_chart(grafico, width="stretch")
    with st.expander(f"Tabela completa ({len(df)} ocupações)"):
        st.dataframe(df, hide_index=True, width="stretch")


def origem(resposta):
    if resposta is None:
        return "falhou"
    if getattr(resposta, "do_cache", False):
        return f"cache (salvo em {fmt_data(resposta.salvo_em)})"
    return "ao vivo"


def aba_comparacao(det):
    st.markdown("**Esta consulta**")
    rest, est, leitos = det["rest"], det["est"], det["leitos"]
    colunas = {
        "REST · detalhe": rest,
        "SOAP · estabelecimento": est,
        "SOAP · leitos": leitos,
    }
    linhas = {
        "Formato": ["JSON", "XML (envelope SOAP 1.2)", "XML (envelope SOAP 1.2)"],
        "Método HTTP": ["GET", "POST", "POST"],
        "Contrato": ["Swagger/OpenAPI (opcional)", "WSDL (obrigatório)", "WSDL (obrigatório)"],
        "Autenticação": ["Nenhuma", "WS-Security UsernameToken", "WS-Security UsernameToken"],
        "Tempo": [fmt_ms(r.tempo_ms) if r else "—" for r in colunas.values()],
        "Tamanho": [fmt_bytes(r.tamanho_bytes) if r else "—" for r in colunas.values()],
        "HTTP": [str(r.status_code) if r else "—" for r in colunas.values()],
        "Tentativas": ["1 (sem novas tentativas)" if rest else "—"]
                      + [str(r.tentativas) if r else "—" for r in (est, leitos)],
        "Origem": [origem(r) for r in colunas.values()],
    }
    if det["sem_leitos"]:
        linhas["Origem"][2] = "ao vivo (sem leitos: NO_RESULT)"
    tabela = pd.DataFrame([{"": nome, **dict(zip(colunas, valores))} for nome, valores in linhas.items()])
    st.dataframe(tabela, hide_index=True, width="stretch")
    if rest and est and not est.do_cache:
        st.caption(f"Para os mesmos dados cadastrais, o XML do SOAP veio "
                   f"{est.tamanho_bytes / rest.tamanho_bytes:.0f}× maior que o JSON do REST.")

    desenhar_benchmark()


def mediana(df, servico):
    linha = df[df["servico"] == servico]
    if linha.empty or pd.isna(linha.iloc[0]["mediana_ms"]):
        return None
    return float(linha.iloc[0]["mediana_ms"])


def custo_wsdl(df):
    linha = df[df["servico"] == "WSDL CnesService"]
    return None if linha.empty else float(linha.iloc[0]["media_ms"])


def ponto_equilibrio(wsdl, rest, soap):
    """Nº de chamadas a partir do qual o SOAP (WSDL + n chamadas) fica mais
    barato que o REST (n chamadas). None = nunca (REST já é mais rápido)."""
    if None in (wsdl, rest, soap) or rest <= soap:
        return None
    return math.ceil(wsdl / (rest - soap))


def desenhar_benchmark():
    st.divider()
    st.markdown("**Benchmark oficial** (`python3 benchmark.py -n 10`, mediana de 10 chamadas)")
    if not CSV_DEPOIS.exists():
        st.info("Rode `python3 benchmark.py` para gerar resultados/desempenho.csv.")
        return
    depois = ler_benchmark(str(CSV_DEPOIS), CSV_DEPOIS.stat().st_mtime)
    antes = ler_benchmark(str(CSV_ANTES), CSV_ANTES.stat().st_mtime) if CSV_ANTES.exists() else None

    barras = []
    if antes is not None:
        barras.append(("REST sem Session", mediana(antes, "REST detalhe"), COR_REST_ANTES))
    barras += [("REST com Session", mediana(depois, "REST detalhe"), COR_REST),
               ("SOAP estabelecimento", mediana(depois, "SOAP estabelecimento"), COR_SOAP),
               ("SOAP leitos", mediana(depois, "SOAP leitos"), COR_SOAP)]
    barras = [b for b in barras if b[1] is not None]
    df_tempo = pd.DataFrame(barras, columns=["Serviço", "Mediana (ms)", "cor"])

    tamanhos = pd.DataFrame([
        ("REST detalhe (JSON)", depois.loc[depois["servico"] == "REST detalhe", "tamanho_bytes"].max(), COR_REST),
        ("SOAP estabelecimento (XML)", depois.loc[depois["servico"] == "SOAP estabelecimento", "tamanho_bytes"].max(), COR_SOAP),
        ("SOAP leitos (XML)", depois.loc[depois["servico"] == "SOAP leitos", "tamanho_bytes"].max(), COR_SOAP),
    ], columns=["Resposta", "Bytes", "cor"]).dropna()

    def barras_horizontais(df, campo_y, campo_x, titulo, formato):
        df = df.copy()
        if campo_y == "Resposta":
            df = df.sort_values(campo_x, ascending=False).reset_index(drop=True)

        base = alt.Chart(df, title=titulo).encode(
            y=alt.Y(f"{campo_y}:N", sort=None, title=None,
                    axis=alt.Axis(labelLimit=200)),
            x=alt.X(f"{campo_x}:Q", title=campo_x, scale=alt.Scale(domainMin=0)),
            tooltip=[campo_y, alt.Tooltip(f"{campo_x}:Q", format=formato)],
        )
        barra = base.mark_bar(cornerRadiusEnd=4, height=0.8).encode(
            color=alt.Color("cor:N", scale=None)
        )
        rotulo = base.mark_text(align="left", dx=6, dy=0, color="#1f2937", fontSize=11).encode(
            text=alt.Text(f"{campo_x}:Q", format=formato)
        )
        return (barra + rotulo).properties(height=56 * max(1, len(df)))

    c1, c2 = st.columns(2)
    c1.altair_chart(barras_horizontais(df_tempo, "Serviço", "Mediana (ms)",
                                       "Tempo por chamada (mediana)", ",.0f"),
                    width="stretch")
    c2.altair_chart(barras_horizontais(tamanhos, "Resposta", "Bytes",
                                       "Tamanho da resposta (CNES 4884124)", ",.0f"),
                    width="stretch")

    # Ponto de equilíbrio do WSDL
    soap_med = mediana(depois, "SOAP estabelecimento")
    cenarios = [("REST com Session", mediana(depois, "REST detalhe"), custo_wsdl(depois), COR_REST)]
    if antes is not None:
        cenarios.insert(0, ("REST sem Session", mediana(antes, "REST detalhe"), custo_wsdl(antes), COR_REST_ANTES))
    wsdl = custo_wsdl(depois)

    st.markdown("**Ponto de equilíbrio do WSDL**")
    st.caption(f"O SOAP paga uma vez o carregamento do WSDL (~{fmt_ms(wsdl)}) e depois cada "
               "chamada. A partir de quantas chamadas esse custo fixo se paga?")
    textos = []
    for nome, rest_med, wsdl_cenario, _ in cenarios:
        n = ponto_equilibrio(wsdl_cenario, rest_med, soap_med)
        if n is None:
            textos.append(f"- **{nome}** ({fmt_ms(rest_med)}/chamada): o REST já é tão rápido "
                          "quanto o SOAP por chamada → o custo do WSDL **nunca se paga**.")
        else:
            textos.append(f"- **{nome}** ({fmt_ms(rest_med)}/chamada × SOAP {fmt_ms(soap_med)}): "
                          f"o SOAP só fica à frente a partir de **≈ {n} chamadas**.")
    st.markdown("\n".join(textos))

    maior_n = max([ponto_equilibrio(w, r, soap_med) or 0 for _, r, w, _ in cenarios] + [20])
    limite = min(max(40, int(maior_n * 1.3)), 400)
    pontos = []
    for n in range(0, limite + 1, max(1, limite // 80)):
        for nome, rest_med, _, _ in cenarios:
            pontos.append({"Chamadas": n, "Cliente": nome, "Tempo acumulado (s)": n * rest_med / 1000})
        pontos.append({"Chamadas": n, "Cliente": "SOAP (WSDL + chamadas)",
                       "Tempo acumulado (s)": (wsdl + n * soap_med) / 1000})
    dominio = [c[0] for c in cenarios] + ["SOAP (WSDL + chamadas)"]
    cores = [c[3] for c in cenarios] + [COR_SOAP]
    linhas = (
        alt.Chart(pd.DataFrame(pontos), title="Tempo acumulado x número de chamadas")
        .mark_line(strokeWidth=2)
        .encode(
            x=alt.X("Chamadas:Q"),
            y=alt.Y("Tempo acumulado (s):Q"),
            color=alt.Color("Cliente:N", scale=alt.Scale(domain=dominio, range=cores),
                            legend=alt.Legend(orient="top", title=None)),
            tooltip=["Cliente", "Chamadas", alt.Tooltip("Tempo acumulado (s):Q", format=".2f")],
        )
        .properties(height=300)
    )
    st.altair_chart(linhas, width="stretch")
    st.caption("Lição: o desempenho depende também da implementação do cliente. Só trocar "
               "`requests.get` por uma `requests.Session` (conexão reaproveitada) mudou a conclusão.")


def cabecalho_ws_security(xml_enviado):
    """Extrai só o <soap:Header> (com o WS-Security) do envelope enviado."""
    try:
        raiz = etree.fromstring(xml_enviado.encode("utf-8"))
        header = next(el for el in raiz if etree.QName(el).localname == "Header")
        return etree.tostring(header, pretty_print=True, encoding="unicode")
    except Exception:
        return None


def mostrar_xml(texto, nome_arquivo, chave):
    if len(texto) > LIMITE_XML_NA_TELA:
        st.caption(f"Mostrando os primeiros {LIMITE_XML_NA_TELA:,} de {len(texto):,} caracteres "
                   "— o download traz o arquivo inteiro.".replace(",", "."))
        st.code(texto[:LIMITE_XML_NA_TELA] + "\n<!-- ... (cortado na tela) -->", language="xml")
    else:
        st.code(texto, language="xml")
    st.download_button(f"⬇️ Baixar {nome_arquivo}", texto, file_name=nome_arquivo,
                       mime="application/xml", key=chave)


def aba_mensagens(det):
    cnes = soap_client.formatar_codigo_cnes(det["cnes"])

    st.markdown("#### REST — JSON recebido")
    if det["rest"]:
        st.caption(f"`GET {det['rest'].url}` — sem corpo na requisição e sem autenticação.")
        json_bonito = json.dumps(json.loads(det["rest"].json_bruto), ensure_ascii=False, indent=2)
        st.code(json_bonito, language="json")
        st.download_button("⬇️ Baixar JSON", json_bonito, file_name=f"rest_{cnes}.json",
                           mime="application/json", key="dl_json")
    else:
        st.warning("Sem resposta REST.")

    st.divider()
    st.markdown("#### SOAP — envelopes XML")
    operacao = st.radio("Operação", ["consultarEstabelecimentoSaude", "consultarLeitosCNES"],
                        horizontal=True, key="op_xml")
    resposta = det["est"] if operacao == "consultarEstabelecimentoSaude" else det["leitos"]
    xml_recebido = det["xml_est"] if operacao == "consultarEstabelecimentoSaude" else det["xml_leitos"]
    if resposta is None:
        if operacao == "consultarLeitosCNES" and det["sem_leitos"]:
            st.info("O serviço respondeu com a falha NO_RESULT: este estabelecimento não tem leitos.")
        else:
            st.warning("Sem resposta SOAP para esta operação.")
        return

    st.markdown("**XML ENVIADO** — `POST` com o envelope no corpo")
    header = cabecalho_ws_security(resposta.xml_enviado)
    if header:
        st.markdown("🔐 **Cabeçalho WS-Security** — é a maior diferença para o REST: o usuário "
                    "e a senha (pública) vão *dentro* do XML, em texto puro, protegidos só pelo HTTPS.")
        st.code(header, language="xml")
    with st.expander("Envelope enviado completo", expanded=False):
        mostrar_xml(resposta.xml_enviado, f"soap_{operacao}_{cnes}_enviado.xml", f"dl_env_{operacao}")

    st.markdown("**XML RECEBIDO** — dados pessoais de profissionais mascarados (LGPD)")
    if resposta.do_cache:
        st.caption(f"Resposta do cache, salva em {fmt_data(resposta.salvo_em)}.")
    mostrar_xml(xml_recebido, f"soap_{operacao}_{cnes}_recebido.xml", f"dl_rec_{operacao}")


def desenhar_detalhe(tipos):
    det = st.session_state.detalhe
    nome = None
    if det["est"]:
        nome = soap_client.resumir_estabelecimento(det["est"].dados).get("nome_fantasia")
    elif det["rest"]:
        nome = det["rest"].dados.get("nome_fantasia")
    st.subheader(f"CNES {soap_client.formatar_codigo_cnes(det['cnes'])} — {nome or 'detalhes'}")
    avisos_soap(det)

    abas = st.tabs(["📋 Visão geral", "🛏️ Leitos (SOAP)", "👩‍⚕️ Profissionais",
                    "⚖️ REST x SOAP", "📨 Mensagens brutas"])
    with abas[0]:
        aba_visao_geral(det, tipos)
    with abas[1]:
        aba_leitos(det)
    with abas[2]:
        aba_profissionais(det)
    with abas[3]:
        aba_comparacao(det)
    with abas[4]:
        aba_mensagens(det)


# Rodapé -----------------------------------------------------------------------

def desenhar_rodape():
    st.divider()
    st.caption(
        "**Fontes:** REST — [API de Dados Abertos da Saúde](https://apidadosabertos.saude.gov.br/v1/#/) "
        f"([swagger.json](https://apidadosabertos.saude.gov.br/static/swagger.json)) · "
        f"SOAP — [WSDL CnesService]({soap_client.WSDL_ESTABELECIMENTO}) e "
        f"[WSDL LeitoService]({soap_client.WSDL_LEITOS}) (barramento CNES/DATASUS). "
        "Dados do Cadastro Nacional de Estabelecimentos de Saúde (CNES) — Ministério da Saúde."
    )


# Programa principal -----------------------------------------------------------

def main():
    iniciar_estado()
    parametros = desenhar_barra_lateral()

    st.title("🏥 Monitor de Estabelecimentos de Saúde (CNES)")
    st.caption("Lista via **REST** (JSON) · detalhe e leitos via **SOAP** (XML/WSDL) · "
               "comparação de tempo e tamanho entre os dois protocolos.")

    # Pré-carrega os WSDLs (uma vez por servidor), para o 1º clique não demorar
    try:
        with st.spinner("Carregando contrato WSDL do SOAP..."):
            soap = preparar_soap()
        origem_wsdl = "do cache em disco" if soap["do_disco"] else "baixados pela rede"
        st.caption(f"✅ Contratos WSDL carregados em {fmt_ms(soap['total_ms'])} ({origem_wsdl}: "
                   + ", ".join(f"{k} {fmt_ms(v)}" for k, v in soap["tempos"].items()) + ").")
    except soap_client.ErroAPISOAP as erro:
        st.warning(f"Não foi possível carregar o WSDL do SOAP agora: {erro} "
                   "A busca REST continua funcionando.")

    if st.button("Buscar estabelecimentos (REST)", type="primary"):
        with st.spinner("Consultando a API REST..."):
            nova_busca(parametros)

    if st.session_state.erro_lista:
        st.error(f"API REST: {st.session_state.erro_lista}")
    if st.session_state.lista is not None:
        desenhar_lista()
        desenhar_mapa()
        desenhar_selecao()
    elif not st.session_state.erro_lista:
        st.info("Escolha o município e o tipo de unidade na barra lateral e clique em "
                "**Buscar estabelecimentos (REST)**.")

    if st.session_state.detalhe:
        try:
            tipos = dict(carregar_tipos_unidade())
        except rest_client.ErroAPIREST:
            tipos = {}
        desenhar_detalhe({k: v.title() for k, v in tipos.items()})

    desenhar_rodape()


main()
