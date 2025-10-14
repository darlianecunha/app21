# -*- coding: utf-8 -*-
import os
import monitor_promos_milhas as m

# Ajusta caminhos para o ambiente atual (CI/local)
m.ARQ_CREDENCIAIS = os.path.abspath("credenciais.txt")
m.ARQ_LOG_CSV = os.path.abspath("monitor_promos_milhas_log.csv")

# Executa
if __name__ == "__main__":
    m.main()
