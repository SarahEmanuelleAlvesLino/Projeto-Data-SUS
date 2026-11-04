# soap_client.py
# Cliente SOAP 1.2 (XML/WSDL) do barramento CNES do DATASUS
# WSDLs:
#   https://servicos.saude.gov.br/cnes/CnesService/v1r0?wsdl  -> consultarEstabelecimentoSaude
#   https://servicos.saude.gov.br/cnes/LeitoService/v1r0?wsdl -> consultarLeitosCNES
# Autenticação: WS-Security UsernameToken (usuário público de consulta)
# Biblioteca: zeep
