# rest_client.py
# Cliente REST (JSON) da API de Dados Abertos da Saúde
# Base: https://apidadosabertos.saude.gov.br
# Vai conter funções para:
#   - listar estabelecimentos de um município (/cnes/estabelecimentos, com limit/offset)
#   - buscar um estabelecimento pelo código CNES (/cnes/estabelecimentos/{codigo_cnes})
# Biblioteca: requests
