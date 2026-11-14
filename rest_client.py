# rest_client.py
# =============================================================================
# Cliente REST (JSON) da API de Dados Abertos da Saúde (Ministério da Saúde)
#
#   Base:     https://apidadosabertos.saude.gov.br
#   Swagger:  https://apidadosabertos.saude.gov.br/v1/#/
#
# Endpoints usados:
#   GET /cnes/estabelecimentos                 -> lista (com filtros e paginação)
#   GET /cnes/estabelecimentos/{codigo_cnes}   -> detalhe de um estabelecimento
#
# =============================================================================

import json
import time
from dataclasses import dataclass
from typing import Any, Optional

import requests

# Configurações ----------------------------------------------------------------------------

BASE_URL = "https://apidadosabertos.saude.gov.br"

# Tempo máximo (em segundos) que esperamos o servidor responder.
TIMEOUT_PADRAO = 20
LIMITE_MAXIMO = 20
CABECALHOS = {
    "Accept": "application/json",
    "User-Agent": "monitor-saude/1.0 (trabalho academico)",
}

# Sessão HTTP reaproveitada entre as chamadas.
# Com requests.get() "solto", cada chamada abria uma conexão TCP + TLS nova
# (~150-250 ms só de handshake). A Session mantém a conexão aberta
# (keep-alive) e a reaproveita, como o zeep já fazia no SOAP.
_sessao = requests.Session()
_sessao.headers.update(CABECALHOS)

# Exceção própria -----------------------------------------------------------------------------

class ErroAPIREST(Exception):

    def __init__(self, mensagem: str, status_code: Optional[int] = None):
        super().__init__(mensagem)
        self.status_code = status_code

# Estrutura do resultado ------------------------------------------------------------------------

@dataclass
class RespostaREST:
    """Resultado de uma chamada REST.

    dados         -> JSON já convertido para Python 
    json_bruto    -> texto original recebido do servidor
    tempo_ms      -> tempo total da requisição, em milissegundos
    tamanho_bytes -> tamanho do corpo da resposta, em bytes
    status_code   -> código HTTP 
    url           -> URL final chamada (com os parâmetros)
    """

    dados: Any
    json_bruto: str
    tempo_ms: float
    tamanho_bytes: int
    status_code: int
    url: str

# Funções auxiliares ----------------------------------------------------------------------

def normalizar_codigo_municipio(codigo) -> int:
    """Converte o código IBGE do município para o formato aceito pela API.
    O IBGE usa 7 dígitos, o último é um dígito verificador. A API do CNES usa só os 6 primeiros.
    """
    texto = str(codigo).strip()
    if not texto.isdigit() or len(texto) not in (6, 7):
        raise ErroAPIREST(
            f"Código de município inválido: '{codigo}'. "
            "Informe o código IBGE com 6 ou 7 dígitos (ex.: 355030 ou 3550308)."
        )
    return int(texto[:6])


def _requisitar(caminho: str, params: Optional[dict] = None) -> RespostaREST:
    # Faz um GET na API e devolve um RespostaREST (função interna).
    url = BASE_URL + caminho

    inicio = time.perf_counter()
    try:
        resposta = _sessao.get(url, params=params, timeout=TIMEOUT_PADRAO)
    except requests.exceptions.Timeout:
        raise ErroAPIREST(
            f"O servidor da API REST não respondeu em {TIMEOUT_PADRAO} segundos. "
            "Tente novamente em instantes."
        )
    except requests.exceptions.SSLError:
        raise ErroAPIREST(
            "Falha na verificação do certificado SSL do servidor REST. "
            "A conexão foi recusada por segurança."
        )
    except requests.exceptions.ConnectionError:
        raise ErroAPIREST(
            "Não foi possível conectar à API REST. "
            "Verifique sua conexão com a internet."
        )
    except requests.exceptions.RequestException as erro:
        raise ErroAPIREST(f"Erro inesperado na requisição REST: {erro}")
    tempo_ms = (time.perf_counter() - inicio) * 1000

    if resposta.status_code >= 500:
        raise ErroAPIREST(
            f"O servidor da API REST está com problemas (HTTP {resposta.status_code}). "
            "Tente novamente mais tarde.",
            resposta.status_code,
        )
    if resposta.status_code >= 400 and resposta.status_code != 404:
        raise ErroAPIREST(
            f"A API REST recusou a requisição (HTTP {resposta.status_code}). "
            "Verifique os parâmetros informados.",
            resposta.status_code,
        )

    try:
        dados = resposta.json()
    except ValueError:
        if resposta.status_code == 404:
            dados = None
        else:
            raise ErroAPIREST(
                "A API REST respondeu, mas o conteúdo não é um JSON válido.",
                resposta.status_code,
            )

    return RespostaREST(
        dados=dados,
        json_bruto=resposta.text,
        tempo_ms=tempo_ms,
        tamanho_bytes=len(resposta.content),
        status_code=resposta.status_code,
        url=resposta.url,
    )

# Funções públicas --------------------------------------------------------------------

def listar_estabelecimentos(
    codigo_municipio=None,
    codigo_uf: Optional[int] = None,
    limit: int = 20,
    offset: int = 0,       # número da PÁGINA, começando em 0 (como diz o swagger).
                           # Testado em 11/11: com limit=5, offset=1 traz os itens 6 a 10.
                           # (Em 03/11 a API se comportou como "itens a pular".)
    codigo_tipo_unidade: Optional[int] = None,
    status: Optional[int] = None,
) -> RespostaREST:

    if not 1 <= int(limit) <= LIMITE_MAXIMO:
        raise ErroAPIREST(f"O limit deve estar entre 1 e {LIMITE_MAXIMO}.")
    if int(offset) < 0:
        raise ErroAPIREST("O offset não pode ser negativo.")

    params = {"limit": int(limit), "offset": int(offset)}
    if codigo_municipio is not None:
        params["codigo_municipio"] = normalizar_codigo_municipio(codigo_municipio)
    if codigo_uf is not None:
        params["codigo_uf"] = int(codigo_uf)
    if codigo_tipo_unidade is not None:
        params["codigo_tipo_unidade"] = int(codigo_tipo_unidade)
    if status is not None:
        params["status"] = int(status)

    resultado = _requisitar("/cnes/estabelecimentos", params)

    if resultado.status_code == 404:
        raise ErroAPIREST("Endpoint de listagem não encontrado (HTTP 404).", 404)

    if not isinstance(resultado.dados, dict) or "estabelecimentos" not in resultado.dados:
        raise ErroAPIREST("Formato inesperado na resposta da listagem REST.")
    resultado.dados = resultado.dados["estabelecimentos"]
    return resultado


def buscar_estabelecimento(codigo_cnes) -> RespostaREST:

    texto = str(codigo_cnes).strip()
    if not texto.isdigit() or len(texto) > 7:
        raise ErroAPIREST(
            f"Código CNES inválido: '{codigo_cnes}'. Use somente números (até 7 dígitos)."
        )

    resultado = _requisitar(f"/cnes/estabelecimentos/{int(texto)}")

    if resultado.status_code == 404:
        raise ErroAPIREST(
            f"Estabelecimento com código CNES {texto} não foi encontrado.", 404
        )
    return resultado


def listar_tipos_unidade() -> RespostaREST:
    # Tipos de unidade (/cnes/tipounidades). Em dados vem a LISTA, ex.:
    # [{"codigo_tipo_unidade": 5, "descricao_tipo_unidade": "HOSPITAL GERAL"}, ...]
    resultado = _requisitar("/cnes/tipounidades")
    if not isinstance(resultado.dados, dict) or "tipos_unidade" not in resultado.dados:
        raise ErroAPIREST("Formato inesperado na resposta de tipos de unidade.")
    resultado.dados = resultado.dados["tipos_unidade"]
    return resultado

# Teste pelo terminal ------------------------------------------------------------

if __name__ == "__main__":
    MUNICIPIO = 3550308  

    def metricas(r: RespostaREST) -> str:
        return f"HTTP {r.status_code} | {r.tempo_ms:.0f} ms | {r.tamanho_bytes} bytes"

    try:
        # Listagem
        print(f"== Listando 5 estabelecimentos do município {MUNICIPIO} (São Paulo) ==")
        lista = listar_estabelecimentos(codigo_municipio=MUNICIPIO, limit=5)
        print("URL:", lista.url)
        print("Métricas:", metricas(lista))
        print()
        for e in lista.dados:
            endereco = (
                f"{e.get('endereco_estabelecimento') or ''}, "
                f"{e.get('numero_estabelecimento') or 's/n'} - "
                f"{e.get('bairro_estabelecimento') or ''}"
            )
            print(f"  {e['codigo_cnes']:>7} | {e.get('nome_fantasia')}")
            print(f"          {endereco}")
        print()

        # Detalhe do primeiro da lista
        if lista.dados:
            cnes = lista.dados[0]["codigo_cnes"]
            print(f"== Detalhe do estabelecimento CNES {cnes} ==")
            det = buscar_estabelecimento(cnes)
            print("URL:", det.url)
            print("Métricas:", metricas(det))
            print(json.dumps(det.dados, ensure_ascii=False, indent=2))
            print()

        print("== Teste de erro: CNES inexistente (0000001) ==")
        try:
            buscar_estabelecimento("0000001")
        except ErroAPIREST as erro:
            print(f"ErroAPIREST capturado (HTTP {erro.status_code}): {erro}")

    except ErroAPIREST as erro:
        print(f"ERRO: {erro}")