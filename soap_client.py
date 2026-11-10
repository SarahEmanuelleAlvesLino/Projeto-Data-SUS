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


import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

import requests
from lxml import etree
from zeep import Client, Settings
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



class ErroAPISOAP(Exception):
    def __init__(self, mensagem: str, status_code: Optional[int] = None,
                 codigo_falha: Optional[str] = None):
        super().__init__(mensagem)
        self.status_code = status_code
        self.codigo_falha = codigo_falha

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
    """

    dados: Any
    xml_bruto: str
    xml_enviado: str
    tempo_ms: float
    tempo_http_ms: float
    tamanho_bytes: int
    status_code: int

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


def _obter_cliente(nome: str):
    if nome in _clientes:
        return _clientes[nome]

    config = _CONFIG_SERVICOS[nome]
    historico = HistoryPlugin()                   # guarda o último XML enviado/recebido
    transporte = TransporteMedido(
        timeout=TIMEOUT_PADRAO,                   # timeout para baixar o WSDL/XSDs
        operation_timeout=TIMEOUT_PADRAO,         # timeout das operações SOAP
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
            f"O WSDL do serviço SOAP não foi baixado em {TIMEOUT_PADRAO} segundos."
        )
    except requests.exceptions.SSLError:
        raise ErroAPISOAP(
            "Falha na verificação do certificado SSL ao baixar o WSDL. "
            "A conexão foi recusada por segurança."
        )
    except requests.exceptions.ConnectionError:
        raise ErroAPISOAP(
            "Não foi possível conectar ao servidor SOAP para baixar o WSDL. "
            "Verifique sua conexão com a internet."
        )
    except Exception as erro:
        raise ErroAPISOAP(f"Não foi possível carregar o WSDL ({config['wsdl']}): {erro}")
    _tempos_wsdl_ms[nome] = (time.perf_counter() - inicio) * 1000

    _clientes[nome] = (cliente, transporte, historico)
    return _clientes[nome]


def carregar_clientes() -> dict:

    for nome in _CONFIG_SERVICOS:
        _obter_cliente(nome)
    return tempos_carregamento_wsdl()


def tempos_carregamento_wsdl() -> dict:
    """Tempo (ms) que cada WSDL levou para ser carregado, ex.:
    {"estabelecimento": 4680.2, "leitos": 587.1}"""
    return dict(_tempos_wsdl_ms)

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
    if isinstance(valor, list):
        return [_simplificar(v) for v in valor]
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
    if falha.detail is None:
        return None
    for elemento in falha.detail.iter():
        if isinstance(elemento.tag, str) and etree.QName(elemento).localname == "codigo":
            return (elemento.text or "").strip() or None
    return None


def _chamar(nome_servico: str,nome_operacao: str, codigo_cnes,msg_nao_encontrado: str) -> RespostaSOAP:
    codigo = formatar_codigo_cnes(codigo_cnes)
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
            raise ErroAPISOAP(
                f"O serviço SOAP retornou uma falha (SOAP Fault): {texto}",
                status, codigo_falha,
            )

        except TransportError as erro:
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
                "Tente novamente em instantes."
            )
        except requests.exceptions.SSLError:
            raise ErroAPISOAP(
                "Falha na verificação do certificado SSL do servidor SOAP. "
                "A conexão foi recusada por segurança."
            )
        except requests.exceptions.ConnectionError:
            raise ErroAPISOAP(
                "Não foi possível conectar ao serviço SOAP. "
                "Verifique sua conexão com a internet."
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
    )




# Funções públicas --------------------------------------------

def consultar_estabelecimento(codigo_cnes) -> RespostaSOAP:
    return _chamar(
        "estabelecimento", "consultarEstabelecimentoSaude", codigo_cnes,
        "Estabelecimento com código CNES {codigo} não foi encontrado no serviço SOAP.",
    )


def consultar_leitos(codigo_cnes) -> RespostaSOAP:
    return _chamar(
        "leitos", "consultarLeitosCNES", codigo_cnes,
        "Nenhum leito encontrado para o CNES {codigo}: o estabelecimento não "
        "existe ou não tem leitos cadastrados.",
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
                print(f"   {rotulo:<38}{resp.status_code:>5}{resp.tempo_ms:>8.0f}ms"
                      f"{resp.tempo_http_ms:>8.0f}ms{resp.tamanho_bytes:>10} B")
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