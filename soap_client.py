# Cliente SOAP (XML / WSDL) do barramento do CNES (DATASUS / Ministério da Saúde)
#
# Protocolo: SOAP 1.2, estilo document/literal
#
# Serviços (cada um descrito por um arquivo WSDL):
#   CnesService  -> https://servicos.saude.gov.br/cnes/CnesService/v1r0?wsdl
#                   operação usada: consultarEstabelecimentoSaude(CodigoCNES)
#   LeitoService -> https://servicos.saude.gov.br/cnes/LeitoService/v1r0?wsdl
#                   operação usada: consultarLeitosCNES(CodigoCNES)
#  --------------------------------------------------------------------------


import json
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import requests
from lxml import etree
from zeep import Client, Settings
from zeep.cache import SqliteCache
from zeep.exceptions import Fault, TransportError, XMLParseError
from zeep.helpers import serialize_object
from zeep.plugins import HistoryPlugin
from zeep.transports import Transport
from zeep.wsse.username import UsernameToken


# Configurações --------------------------------------------------------------------------

WSDL_ESTABELECIMENTO = "https://servicos.saude.gov.br/cnes/CnesService/v1r0?wsdl"
WSDL_LEITOS = "https://servicos.saude.gov.br/cnes/LeitoService/v1r0?wsdl"


USUARIO_PUBLICO = "CNES.PUBLICO"
SENHA_PUBLICA = "cnes#2015public"

# Tempo máximo de espera, em segundos
TIMEOUT_PADRAO = 30

# Novas tentativas: o barramento do DATASUS às vezes responde 504 Gateway
# Timeout. Só repetimos erros TRANSITÓRIOS (rede, 502/503/504); erros
# definitivos (CNES inexistente, senha errada) não adianta repetir.
TENTATIVAS_MAXIMAS = 3
ESPERA_ENTRE_TENTATIVAS = 1.0          # segundos; cresce a cada tentativa (1 s, 2 s)
HTTP_TRANSITORIOS = {502: "Bad Gateway", 503: "Service Unavailable", 504: "Gateway Timeout"}

# Cache em disco (pasta cache/, fora do Git):
#   cache/wsdl.sqlite   -> WSDLs e XSDs baixados (o app abre mesmo se o WSDL estiver fora)
#   cache/respostas/    -> última resposta boa de cada CNES; usada só se o servidor
#                          falhar depois de todas as tentativas
PASTA_CACHE = Path(__file__).resolve().parent / "cache"
VALIDADE_CACHE_WSDL = 7 * 24 * 3600    # 7 dias, em segundos



class ErroAPISOAP(Exception):
    """transitorio=True indica falha passageira do servidor (vale tentar de novo)."""

    def __init__(self, mensagem: str, status_code: Optional[int] = None,
                 codigo_falha: Optional[str] = None, transitorio: bool = False):
        super().__init__(mensagem)
        self.status_code = status_code
        self.codigo_falha = codigo_falha
        self.transitorio = transitorio
        self.tentativas = 1

# Estrutura do resultado ---------------------------------------------------------

@dataclass
class RespostaSOAP:
    """Resultado de uma chamada SOAP.

    dados         -> resposta convertida para dict/list Python
    xml_bruto     -> XML recebido, formatado com indentação
    xml_enviado   -> envelope XML que enviado
    tempo_ms      -> tempo da chamada da operação: rede + o zeep montar o XML
                     enviado e interpretar o XML recebido. NÃO inclui o
                     carregamento do WSDL, que é feito só uma vez.
    tempo_http_ms -> só a parte de rede (envio + espera + download). A
                     diferença tempo_ms - tempo_http_ms é o custo de
                     processar o XML no nosso lado.
    tamanho_bytes -> tamanho real do corpo da resposta HTTP, em bytes
    status_code   -> código HTTP
    tentativas    -> quantas tentativas foram feitas até dar certo
    do_cache      -> True se o servidor falhou e os dados vieram de cache/
    salvo_em      -> data/hora em que a resposta foi obtida do servidor
    """

    dados: Any
    xml_bruto: str
    xml_enviado: str
    tempo_ms: float
    tempo_http_ms: float
    tamanho_bytes: int
    status_code: int
    tentativas: int = 1
    do_cache: bool = False
    salvo_em: Optional[str] = None

# Transporte que guarda a resposta HTTP --------------------------------------------------------------------------

class TransporteMedido(Transport):
    # Transporte HTTP do zeep que guarda a última resposta recebida.

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ultima_resposta = None
        self.ultimo_tempo_http_ms = 0.0

    def post(self, address, message, headers):
        self.ultima_resposta = None
        inicio = time.perf_counter()
        resposta = super().post(address, message, headers)
        self.ultimo_tempo_http_ms = (time.perf_counter() - inicio) * 1000
        self.ultima_resposta = resposta
        return resposta



# Clientes zeep (criados uma vez) -----------------------------------------
_CONFIG_SERVICOS = {
    "estabelecimento": {"wsdl": WSDL_ESTABELECIMENTO, "strict": False},
    "leitos": {"wsdl": WSDL_LEITOS, "strict": True},
}

_clientes = {}                   # nome -> (Client, TransporteMedido, HistoryPlugin)
_tempos_wsdl_ms = {}             # nome -> tempo de carregamento do WSDL (ms)
_trava = threading.Lock()        # evita duas chamadas misturando o histórico
                                 # (o Streamlit pode atender abas em paralelo)


def _criar_cliente(nome: str, usar_cache_wsdl: bool = True):
    # Cria um cliente zeep novo e mede o tempo de carregamento do WSDL.
    config = _CONFIG_SERVICOS[nome]
    historico = HistoryPlugin()                   # guarda o último XML enviado/recebido
    cache_wsdl = None
    if usar_cache_wsdl:
        PASTA_CACHE.mkdir(exist_ok=True)
        cache_wsdl = SqliteCache(path=str(PASTA_CACHE / "wsdl.sqlite"),
                                 timeout=VALIDADE_CACHE_WSDL)
    transporte = TransporteMedido(
        timeout=TIMEOUT_PADRAO,                   # timeout para baixar o WSDL/XSDs
        operation_timeout=TIMEOUT_PADRAO,         # timeout das operações SOAP
        cache=cache_wsdl,
    )                                             # (SSL é verificado por padrão)
    configuracoes = Settings(strict=config["strict"], xml_huge_tree=True)

    inicio = time.perf_counter()
    try:
        cliente = Client(
            config["wsdl"],
            wsse=UsernameToken(USUARIO_PUBLICO, SENHA_PUBLICA),  # WS-Security
            transport=transporte,
            plugins=[historico],
            settings=configuracoes,
        )
    except requests.exceptions.Timeout:
        raise ErroAPISOAP(
            f"O WSDL do serviço SOAP não foi baixado em {TIMEOUT_PADRAO} segundos.",
            transitorio=True,
        )
    except requests.exceptions.SSLError:
        raise ErroAPISOAP(
            "Falha na verificação do certificado SSL ao baixar o WSDL. "
            "A conexão foi recusada por segurança."
        )
    except requests.exceptions.ConnectionError:
        raise ErroAPISOAP(
            "Não foi possível conectar ao servidor SOAP para baixar o WSDL. "
            "Verifique sua conexão com a internet.",
            transitorio=True,
        )
    except Exception as erro:
        # Ex.: o servidor devolveu uma página de erro (504) no lugar do WSDL
        raise ErroAPISOAP(
            f"Não foi possível carregar o WSDL ({config['wsdl']}): {erro}",
            transitorio=True,
        )
    tempo_ms = (time.perf_counter() - inicio) * 1000
    return (cliente, transporte, historico), tempo_ms


def _obter_cliente(nome: str):
    if nome not in _clientes:
        _clientes[nome], _tempos_wsdl_ms[nome] = _criar_cliente(nome)
    return _clientes[nome]


def carregar_clientes() -> dict:

    for nome in _CONFIG_SERVICOS:
        _obter_cliente(nome)
    return tempos_carregamento_wsdl()


def tempos_carregamento_wsdl() -> dict:
    """Tempo (ms) que cada WSDL levou para ser carregado, ex.:
    {"estabelecimento": 4680.2, "leitos": 587.1}
    Obs.: com o cache de WSDL, a partir da 2ª execução o tempo é o de ler do disco."""
    return dict(_tempos_wsdl_ms)


def medir_carregamento_wsdl(nome: str, usar_cache_wsdl: bool = False) -> float:
    """Carrega o WSDL num cliente descartável e devolve o tempo (ms).
    Sem cache, mede o custo real de baixar e interpretar o WSDL pela rede."""
    _, tempo_ms = _criar_cliente(nome, usar_cache_wsdl=usar_cache_wsdl)
    return tempo_ms

# Funções auxiliares -------------------------------------------------------


def formatar_codigo_cnes(codigo_cnes) -> str:
    texto = str(codigo_cnes).strip()
    if not texto.isdigit() or len(texto) > 7:
        raise ErroAPISOAP(
            f"Código CNES inválido: '{codigo_cnes}'. Use somente números (até 7 dígitos)."
        )
    return texto.zfill(7)


def _simplificar(valor):
    #Converte recursivamente o resultado do zeep para tipos simples.

    if isinstance(valor, dict):
        return {chave: _simplificar(v) for chave, v in valor.items()}
    if isinstance(valor, (list, tuple, deque)):
        return [_simplificar(v) for v in valor]
    if isinstance(valor, etree._Element):
        # Elemento que o WSDL não descreve (strict=False guarda em _raw_elements,
        # ex.: fluxoClientela). Vira {nome: texto} ou {nome: {filhos...}}.
        nome = etree.QName(valor).localname
        filhos = [f for f in valor if isinstance(f.tag, str)]
        if not filhos:
            return {nome: (valor.text or "").strip() or None}
        conteudo = {}
        for f in filhos:
            conteudo.update(_simplificar(f))
        return {nome: conteudo}
    if isinstance(valor, (datetime, date)):
        return valor.isoformat()
    if isinstance(valor, Decimal):
        return int(valor) if valor == valor.to_integral_value() else float(valor)
    return valor


def _xml_formatado(conteudo) -> str:
    """Devolve o XML com indentação (pretty print), como texto."""
    try:
        if isinstance(conteudo, (bytes, str)):
            arvore = etree.fromstring(conteudo if isinstance(conteudo, bytes) else conteudo.encode())
        else:
            arvore = conteudo  # já é um elemento lxml (ex.: vindo do HistoryPlugin)
        return etree.tostring(arvore, pretty_print=True, encoding="unicode")
    except Exception:
        # Se não for um XML válido, devolve como veio
        return conteudo.decode("utf-8", "replace") if isinstance(conteudo, bytes) else str(conteudo)


def _codigo_da_falha(falha: Fault) -> Optional[str]:
    # Procura o <codigo> do MSFalha (ex.: NO_RESULT) dentro do detail.
    # Atenção: quando a resposta NÃO é um SOAP Fault de verdade (ex.: página
    # HTML do 504), o zeep coloca no detail o documento inteiro em BYTES,
    # e não um elemento XML. Por isso convertemos antes de procurar.
    detalhe = falha.detail
    if detalhe is None:
        return None
    if isinstance(detalhe, (bytes, str)):
        try:
            detalhe = etree.fromstring(detalhe if isinstance(detalhe, bytes) else detalhe.encode())
        except Exception:
            return None
    if not hasattr(detalhe, "iter"):
        return None
    for elemento in detalhe.iter():
        if isinstance(elemento.tag, str) and etree.QName(elemento).localname == "codigo":
            return (elemento.text or "").strip() or None
    return None


def _mensagem_instabilidade(status: Optional[int]) -> str:
    nome = HTTP_TRANSITORIOS.get(status, "erro no servidor")
    return (
        f"O barramento SOAP do DATASUS está instável (HTTP {status} {nome}): "
        "ele devolveu uma página de erro no lugar do envelope SOAP. "
        "Isso é um problema do servidor, não da consulta. Tente novamente em instantes."
    )


def _chamar_uma_vez(nome_servico: str, nome_operacao: str, codigo: str,
                    msg_nao_encontrado: str) -> RespostaSOAP:
    # Uma única tentativa. Erros viram ErroAPISOAP (transitorio=True ou False).
    cliente, transporte, historico = _obter_cliente(nome_servico)
    operacao = getattr(cliente.service, nome_operacao)

    with _trava:
        inicio = time.perf_counter()
        try:
            resultado = operacao(CodigoCNES={"codigo": codigo})

        except Fault as falha:
            status = transporte.ultima_resposta.status_code if transporte.ultima_resposta is not None else None
            codigo_falha = _codigo_da_falha(falha)
            texto = (falha.message or "").strip()

            if codigo_falha == "NO_RESULT":
                raise ErroAPISOAP(msg_nao_encontrado.format(codigo=codigo), status, codigo_falha)
            if "OSB-386200" in texto or "security" in texto.lower():
                raise ErroAPISOAP(
                    "Falha de autenticação no serviço SOAP (WS-Security): usuário ou "
                    f"senha recusados pelo servidor. Detalhe: {texto}",
                    status, codigo_falha,
                )
            # "Unknown fault occured" = o zeep não achou um <Fault> no envelope,
            # ou seja, a resposta nem era SOAP (ex.: HTML do 504 Gateway Timeout).
            if status in HTTP_TRANSITORIOS or (texto == "Unknown fault occured" and (status or 0) >= 500):
                raise ErroAPISOAP(_mensagem_instabilidade(status), status, codigo_falha,
                                  transitorio=True)
            raise ErroAPISOAP(
                f"O serviço SOAP retornou uma falha (SOAP Fault): {texto}",
                status, codigo_falha,
            )

        except TransportError as erro:
            # Resposta HTTP de erro que o zeep não conseguiu ler como XML
            if erro.status_code in HTTP_TRANSITORIOS:
                raise ErroAPISOAP(_mensagem_instabilidade(erro.status_code),
                                  erro.status_code, transitorio=True)
            raise ErroAPISOAP(
                f"O servidor SOAP respondeu com erro HTTP {erro.status_code}. "
                "Tente novamente mais tarde.",
                erro.status_code,
            )
        except XMLParseError as erro:
            raise ErroAPISOAP(
                f"A resposta SOAP não corresponde ao que o WSDL descreve: {erro}"
            )
        except requests.exceptions.Timeout:
            raise ErroAPISOAP(
                f"O serviço SOAP não respondeu em {TIMEOUT_PADRAO} segundos. "
                "Tente novamente em instantes.",
                transitorio=True,
            )
        except requests.exceptions.SSLError:
            raise ErroAPISOAP(
                "Falha na verificação do certificado SSL do servidor SOAP. "
                "A conexão foi recusada por segurança."
            )
        except requests.exceptions.ConnectionError:
            raise ErroAPISOAP(
                "Não foi possível conectar ao serviço SOAP. "
                "Verifique sua conexão com a internet.",
                transitorio=True,
            )
        tempo_ms = (time.perf_counter() - inicio) * 1000

        resposta_http = transporte.ultima_resposta
        xml_enviado = _xml_formatado(historico.last_sent["envelope"])

    return RespostaSOAP(
        dados=_simplificar(serialize_object(resultado)),
        xml_bruto=_xml_formatado(resposta_http.content),
        xml_enviado=xml_enviado,
        tempo_ms=tempo_ms,
        tempo_http_ms=transporte.ultimo_tempo_http_ms,
        tamanho_bytes=len(resposta_http.content),   # bytes reais do corpo HTTP
        status_code=resposta_http.status_code,
        salvo_em=datetime.now().isoformat(timespec="seconds"),
    )


# Cache de respostas -------------------------------------------------------

def _arquivo_cache(nome_servico: str, codigo: str) -> Path:
    return PASTA_CACHE / "respostas" / f"{nome_servico}_{codigo}.json"


def _salvar_cache(nome_servico: str, codigo: str, resposta: RespostaSOAP) -> None:
    try:
        arquivo = _arquivo_cache(nome_servico, codigo)
        arquivo.parent.mkdir(parents=True, exist_ok=True)
        arquivo.write_text(json.dumps(asdict(resposta), ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # sem cache não é um erro grave: a consulta já deu certo


def _ler_cache(nome_servico: str, codigo: str) -> Optional[RespostaSOAP]:
    try:
        conteudo = json.loads(_arquivo_cache(nome_servico, codigo).read_text(encoding="utf-8"))
        resposta = RespostaSOAP(**conteudo)
    except (OSError, ValueError, TypeError):
        return None
    resposta.do_cache = True
    return resposta


def _chamar(nome_servico: str, nome_operacao: str, codigo_cnes, msg_nao_encontrado: str,
            usar_cache: bool = True) -> RespostaSOAP:
    # Faz até TENTATIVAS_MAXIMAS tentativas para erros transitórios.
    # Se todas falharem e existir uma resposta salva em cache/, devolve ela
    # (com do_cache=True). Use usar_cache=False para medir desempenho.
    codigo = formatar_codigo_cnes(codigo_cnes)
    ultimo_erro = None

    for tentativa in range(1, TENTATIVAS_MAXIMAS + 1):
        try:
            resposta = _chamar_uma_vez(nome_servico, nome_operacao, codigo, msg_nao_encontrado)
        except ErroAPISOAP as erro:
            erro.tentativas = tentativa
            if not erro.transitorio:
                raise                       # erro definitivo: não adianta repetir
            ultimo_erro = erro
            if tentativa < TENTATIVAS_MAXIMAS:
                time.sleep(ESPERA_ENTRE_TENTATIVAS * tentativa)
            continue
        resposta.tentativas = tentativa
        if usar_cache:
            _salvar_cache(nome_servico, codigo, resposta)
        return resposta

    if usar_cache:
        salva = _ler_cache(nome_servico, codigo)
        if salva is not None:
            salva.tentativas = TENTATIVAS_MAXIMAS
            return salva
    raise ultimo_erro




# Funções públicas --------------------------------------------

def consultar_estabelecimento(codigo_cnes, usar_cache: bool = True) -> RespostaSOAP:
    return _chamar(
        "estabelecimento", "consultarEstabelecimentoSaude", codigo_cnes,
        "Estabelecimento com código CNES {codigo} não foi encontrado no serviço SOAP.",
        usar_cache,
    )


def consultar_leitos(codigo_cnes, usar_cache: bool = True) -> RespostaSOAP:
    return _chamar(
        "leitos", "consultarLeitosCNES", codigo_cnes,
        "Nenhum leito encontrado para o CNES {codigo}: o estabelecimento não "
        "existe ou não tem leitos cadastrados.",
        usar_cache,
    )


def resumir_estabelecimento(dados: dict) -> dict:
    def pegar(origem, *caminho):
        for chave in caminho:
            if not isinstance(origem, dict):
                return None
            origem = origem.get(chave)
        return origem

    est = dados.get("EstabelecimentoSaude") or {}
    end = est.get("Endereco") or {}

    partes_endereco = [
        pegar(end, "nomeLogradouro"),
        pegar(end, "numero"),
        pegar(end, "complemento"),
        pegar(end, "Bairro", "descricaoBairro"),
    ]
    telefones = [
        f"({t.get('DDD')}) {t.get('numeroTelefone')}"
        for t in (est.get("Telefone") or []) if t.get("numeroTelefone")
    ]

    return {
        "codigo_cnes": pegar(est, "CodigoCNES", "codigo"),
        "nome_fantasia": pegar(est, "nomeFantasia", "Nome"),
        "razao_social": pegar(est, "nomeEmpresarial", "Nome"),
        "cnpj": pegar(est, "CNPJ", "numeroCNPJ"),
        "tipo_unidade": pegar(est, "tipoUnidade", "descricao"),
        "esfera_administrativa": pegar(est, "esferaAdministrativa", "descricao"),
        "endereco": ", ".join(p for p in partes_endereco if p),
        "cep": pegar(end, "CEP", "numeroCEP"),
        "municipio": pegar(end, "Municipio", "nomeMunicipio"),
        "uf": pegar(end, "Municipio", "UF", "siglaUF"),
        "telefones": telefones,
        "email": pegar(est, "Email", "descricaoEmail"),
        "data_atualizacao": est.get("dataAtualizacao"),
        "sumario": dados.get("sumario") or {},
    }



# Teste ---------------------


if __name__ == "__main__":
    import rest_client

    def linhas(texto: str, n: int = 40) -> str:
        todas = texto.splitlines()
        extra = f"\n    ... (+{len(todas) - n} linhas)" if len(todas) > n else ""
        return "\n".join("    " + l for l in todas[:n]) + extra

    try:
        print("== (a) REST: buscando 1 hospital geral em São Paulo ==")
        lista = rest_client.listar_estabelecimentos(
            codigo_municipio=355030, codigo_tipo_unidade=5, status=1, limit=1
        )
        if not lista.dados:
            raise SystemExit("Nenhum hospital retornado pelo REST.")
        cnes = lista.dados[0]["codigo_cnes"]
        print(f"   codigo_cnes no REST: {cnes!r} (número)  ->  no SOAP: '{formatar_codigo_cnes(cnes)}'")
        print()

        print("== Carregando os WSDLs (uma única vez) ==")
        for nome, ms in carregar_clientes().items():
            print(f"   WSDL {nome:<15} {ms:8.0f} ms")
        print()

        print(f"== (b) SOAP consultarEstabelecimentoSaude({formatar_codigo_cnes(cnes)}) ==")
        est = consultar_estabelecimento(cnes)
        r = resumir_estabelecimento(est.dados)
        print(f"   Nome fantasia: {r['nome_fantasia']}")
        print(f"   Razão social : {r['razao_social']}")
        print(f"   Tipo / esfera: {r['tipo_unidade']} / {r['esfera_administrativa']}")
        print(f"   Endereço     : {r['endereco']} - {r['municipio']}/{r['uf']} - CEP {r['cep']}")
        print(f"   Telefones    : {', '.join(r['telefones']) or '-'}")
        print(f"   Sumário      : {r['sumario']}")
        print()

        print(f"== (c) SOAP consultarLeitosCNES({formatar_codigo_cnes(cnes)}) ==")
        try:
            leitos = consultar_leitos(cnes)
            print(f"   {'Descrição':<35}{'Total':>7}{'SUS':>7}")
            for l in leitos.dados:
                print(f"   {l['descricao']:<35}{l['quantidadeLeito']:>7}{l['quantidadeLeitoSUS']:>7}")
            print(f"   {'TOTAL':<35}{sum(l['quantidadeLeito'] for l in leitos.dados):>7}"
                  f"{sum(l['quantidadeLeitoSUS'] for l in leitos.dados):>7}")
        except ErroAPISOAP as erro:
            leitos = None
            print(f"   {erro}")
        print()

        rest_det = rest_client.buscar_estabelecimento(cnes)
        print("== (d) Tempo e tamanho de cada chamada ==")
        print(f"   {'Chamada':<38}{'HTTP':>5}{'tempo':>10}{'só rede':>10}{'tamanho':>12}")
        print(f"   {'REST  /cnes/estabelecimentos/{cnes}':<38}{rest_det.status_code:>5}"
              f"{rest_det.tempo_ms:>8.0f}ms{'-':>10}{rest_det.tamanho_bytes:>10} B")
        for rotulo, resp in (("SOAP  consultarEstabelecimentoSaude", est),
                             ("SOAP  consultarLeitosCNES", leitos)):
            if resp:
                origem = " (cache)" if resp.do_cache else ""
                print(f"   {rotulo:<38}{resp.status_code:>5}{resp.tempo_ms:>8.0f}ms"
                      f"{resp.tempo_http_ms:>8.0f}ms{resp.tamanho_bytes:>10} B{origem}")
        print("   (WSDLs carregados uma vez:",
              ", ".join(f"{k} {v:.0f} ms" for k, v in tempos_carregamento_wsdl().items()), ")")
        print()

        print("== (e) XML ENVIADO (consultarEstabelecimentoSaude) ==")
        print(linhas(est.xml_enviado))
        print()
        print("== (e) XML RECEBIDO (consultarEstabelecimentoSaude) ==")
        print(linhas(est.xml_bruto))
        print()
        print("== (f) Teste de erro: CNES inexistente (0000001) ==")
        for funcao in (consultar_estabelecimento, consultar_leitos):
            try:
                funcao("0000001")
            except ErroAPISOAP as erro:
                print(f"   {funcao.__name__}: ErroAPISOAP (HTTP {erro.status_code}, "
                      f"código {erro.codigo_falha}): {erro}")

    except (ErroAPISOAP, rest_client.ErroAPIREST) as erro:
        print(f"ERRO: {erro}")
