# Testes dos clientes REST e SOAP.
#
# Rodar:  python3 -m pytest -v
#
# Há dois tipos de teste:
#   - com o servidor REAL (precisam de internet). Se o SOAP estiver fora do
#     ar (erro transitório, ex.: 504), o teste é PULADO (skip), não reprovado:
#     servidor fora não é defeito do nosso código.
#   - SIMULADOS com unittest.mock: trocamos o cliente zeep por um falso para
#     testar o tratamento do 504, as novas tentativas e o cache, sem rede.
# --------------------------------------------------------------------------

from types import SimpleNamespace
from unittest import mock

import pytest
from lxml import etree
from zeep.exceptions import Fault, TransportError

import rest_client
import soap_client

CNES_HOSPITAL = 4884124        # hospital geral em São Paulo (usado nos exemplos)
CNES_INEXISTENTE = "0000001"


def soap_ou_pular(funcao, *args, **kwargs):
    """Chama o SOAP real; se o servidor estiver instável, pula o teste."""
    try:
        return funcao(*args, **kwargs)
    except soap_client.ErroAPISOAP as erro:
        if erro.transitorio:
            pytest.skip(f"SOAP do DATASUS indisponível agora: {erro}")
        raise


# REST (servidor real) ------------------------------------------------------

def test_rest_lista_estabelecimentos():
    r = rest_client.listar_estabelecimentos(codigo_municipio=3550308, limit=5)
    assert r.status_code == 200
    assert isinstance(r.dados, list) and 0 < len(r.dados) <= 5
    # código IBGE de 7 dígitos foi convertido para 6 (355030)
    assert all(e["codigo_municipio"] == 355030 for e in r.dados)
    assert r.tempo_ms > 0 and r.tamanho_bytes > 0


def test_rest_detalhe_estabelecimento():
    r = rest_client.buscar_estabelecimento(CNES_HOSPITAL)
    assert r.status_code == 200
    assert r.dados["codigo_cnes"] == CNES_HOSPITAL
    assert r.dados["nome_fantasia"]
    assert r.tamanho_bytes > 0 and '"codigo_cnes"' in r.json_bruto


def test_rest_cnes_inexistente_da_404_amigavel():
    with pytest.raises(rest_client.ErroAPIREST) as erro:
        rest_client.buscar_estabelecimento(CNES_INEXISTENTE)
    assert erro.value.status_code == 404
    assert "não foi encontrado" in str(erro.value)


# SOAP (servidor real) ------------------------------------------------------

def test_soap_consulta_estabelecimento():
    r = soap_ou_pular(soap_client.consultar_estabelecimento, CNES_HOSPITAL, usar_cache=False)
    assert r.status_code == 200 and not r.do_cache
    resumo = soap_client.resumir_estabelecimento(r.dados)
    assert resumo["codigo_cnes"] == "4884124"
    assert resumo["nome_fantasia"]
    assert "<soap-env:Envelope" in r.xml_enviado and "UsernameToken" in r.xml_enviado
    assert "Envelope" in r.xml_bruto


def test_soap_consulta_leitos():
    r = soap_ou_pular(soap_client.consultar_leitos, CNES_HOSPITAL, usar_cache=False)
    assert r.status_code == 200
    assert isinstance(r.dados, list) and r.dados
    assert {"descricao", "quantidadeLeito", "quantidadeLeitoSUS"} <= set(r.dados[0])


def test_soap_cnes_inexistente():
    with pytest.raises(soap_client.ErroAPISOAP) as erro:
        soap_ou_pular(soap_client.consultar_estabelecimento, CNES_INEXISTENTE, usar_cache=False)
    assert erro.value.codigo_falha == "NO_RESULT"
    assert not erro.value.transitorio            # erro definitivo: sem novas tentativas
    assert erro.value.tentativas == 1
    assert "não foi encontrado" in str(erro.value)


# Código CNES (sem rede) ----------------------------------------------------

@pytest.mark.parametrize("entrada, esperado", [
    (367230, "0367230"),        # número vindo do REST, sem o zero à esquerda
    ("367230", "0367230"),
    ("0367230", "0367230"),
    (4884124, "4884124"),
    (" 1 ", "0000001"),
])
def test_zfill_codigo_cnes(entrada, esperado):
    assert soap_client.formatar_codigo_cnes(entrada) == esperado


@pytest.mark.parametrize("entrada", ["abc", "12345678", "", "36-7230"])
def test_codigo_cnes_invalido(entrada):
    with pytest.raises(soap_client.ErroAPISOAP):
        soap_client.formatar_codigo_cnes(entrada)


# Tratamento do 504 e novas tentativas (simulado, sem rede) ----------------

HTML_504 = (b"<html><head><title>504 Gateway Time-out</title></head>"
            b"<body><h1>504 Gateway Time-out</h1></body></html>")


def cliente_falso(efeitos, status=504):
    """Monta um cliente zeep falso.

    efeitos: lista do que cada chamada da operação faz (uma exceção para
    lançar, ou um valor para devolver).
    """
    transporte = SimpleNamespace(
        ultima_resposta=SimpleNamespace(status_code=status, content=HTML_504),
        ultimo_tempo_http_ms=10.0,
    )

    def efeito(*args, **kwargs):
        atual = efeitos.pop(0)
        if isinstance(atual, Exception):
            transporte.ultima_resposta = SimpleNamespace(status_code=status, content=HTML_504)
            raise atual
        transporte.ultima_resposta = SimpleNamespace(
            status_code=200, content=b"<Envelope><Body><ok>1</ok></Body></Envelope>")
        return atual

    operacao = mock.Mock(side_effect=efeito)
    servico = SimpleNamespace(consultarEstabelecimentoSaude=operacao, consultarLeitosCNES=operacao)
    historico = SimpleNamespace(last_sent={"envelope": etree.fromstring(b"<enviado/>")})
    return (SimpleNamespace(service=servico), transporte, historico), operacao


@pytest.fixture
def sem_espera(tmp_path):
    """Não espera de verdade entre tentativas e usa uma pasta de cache temporária."""
    with mock.patch.object(soap_client.time, "sleep") as dormir, \
         mock.patch.object(soap_client, "PASTA_CACHE", tmp_path):
        yield dormir


def test_504_como_fault_com_detail_em_bytes(sem_espera):
    # CnesService: o zeep lê o HTML como XML, não acha <Fault> e lança
    # Fault("Unknown fault occured") com o documento inteiro em BYTES no detail.
    falha = Fault("Unknown fault occured", detail=HTML_504)
    cliente, operacao = cliente_falso([falha, falha, falha])
    with mock.patch.object(soap_client, "_obter_cliente", return_value=cliente):
        with pytest.raises(soap_client.ErroAPISOAP) as erro:
            soap_client.consultar_estabelecimento(CNES_HOSPITAL)

    assert erro.value.transitorio
    assert erro.value.status_code == 504
    assert "504" in str(erro.value) and "instável" in str(erro.value)
    assert operacao.call_count == soap_client.TENTATIVAS_MAXIMAS == erro.value.tentativas
    assert sem_espera.call_count == soap_client.TENTATIVAS_MAXIMAS - 1


def test_504_como_transport_error(sem_espera):
    # LeitoService: HTML que não é XML válido -> TransportError(504)
    falha = TransportError("Server returned response (504) with invalid XML",
                           status_code=504, content=HTML_504)
    cliente, operacao = cliente_falso([falha, falha, falha])
    with mock.patch.object(soap_client, "_obter_cliente", return_value=cliente):
        with pytest.raises(soap_client.ErroAPISOAP) as erro:
            soap_client.consultar_leitos(CNES_HOSPITAL)

    assert erro.value.transitorio and erro.value.status_code == 504
    assert operacao.call_count == soap_client.TENTATIVAS_MAXIMAS


def test_nova_tentativa_resolve_504(sem_espera):
    falha = TransportError("504", status_code=504, content=HTML_504)
    cliente, operacao = cliente_falso([falha, {"ok": 1}])
    with mock.patch.object(soap_client, "_obter_cliente", return_value=cliente):
        r = soap_client.consultar_leitos(CNES_HOSPITAL)

    assert r.tentativas == 2 and not r.do_cache
    assert r.dados == {"ok": 1}
    assert operacao.call_count == 2


def test_erro_definitivo_nao_repete(sem_espera):
    detalhe = etree.fromstring(
        b'<Detail><MSFalha><Mensagem><codigo>NO_RESULT</codigo></Mensagem></MSFalha></Detail>')
    cliente, operacao = cliente_falso(
        [Fault("Nenhum resultado foi encontrado", detail=detalhe)], status=500)
    with mock.patch.object(soap_client, "_obter_cliente", return_value=cliente):
        with pytest.raises(soap_client.ErroAPISOAP) as erro:
            soap_client.consultar_estabelecimento(CNES_INEXISTENTE)

    assert erro.value.codigo_falha == "NO_RESULT" and not erro.value.transitorio
    assert operacao.call_count == 1
    sem_espera.assert_not_called()


def test_cache_usado_quando_servidor_cai(sem_espera, tmp_path):
    falha = TransportError("504", status_code=504, content=HTML_504)
    cliente, _ = cliente_falso([{"ok": 1}, falha, falha, falha])
    with mock.patch.object(soap_client, "_obter_cliente", return_value=cliente):
        primeira = soap_client.consultar_leitos(CNES_HOSPITAL)       # sucesso: salva em cache/
        segunda = soap_client.consultar_leitos(CNES_HOSPITAL)        # 3x 504: usa o cache

    assert (tmp_path / "respostas" / "leitos_4884124.json").exists()
    assert not primeira.do_cache
    assert segunda.do_cache and segunda.dados == {"ok": 1}
    assert segunda.salvo_em == primeira.salvo_em


def test_codigo_da_falha_aceita_detail_em_bytes():
    xml = b'<Detail><MSFalha><Mensagem><codigo>NO_RESULT</codigo></Mensagem></MSFalha></Detail>'
    assert soap_client._codigo_da_falha(Fault("x", detail=xml)) == "NO_RESULT"
    assert soap_client._codigo_da_falha(Fault("x", detail=HTML_504)) is None
    assert soap_client._codigo_da_falha(Fault("x", detail=b"isso nao e xml <")) is None
    assert soap_client._codigo_da_falha(Fault("x", detail=None)) is None
