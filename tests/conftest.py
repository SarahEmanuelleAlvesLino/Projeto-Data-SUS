# Permite que os testes importem rest_client e soap_client da pasta do projeto.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
