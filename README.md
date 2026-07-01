# 💰 Finance Dashboard

Dashboard financiero personal generado automáticamente desde Notion.
Lee tus budgets, transacciones y cuentas, y genera un `output/dashboard.html` interactivo.

## Estructura

```
finance-dashboard/
├── .env                    # Tu token de Notion (nunca subir a GitHub)
├── .env.example            # Template del .env
├── .gitignore              # Excluye .env, output/, __pycache__
├── requirements.txt        # Dependencias Python
├── generate_dashboard.py   # Script principal
└── output/                 # Generado automáticamente
    ├── dashboard.html
    └── history.json
```

## Setup

```bash
# 1. Crear entorno virtual
python3 -m venv .venv
source .venv/bin/activate   # Mac/Linux
# .venv\Scripts\activate    # Windows

# 2. Instalar dependencias
pip install -r requirements.txt

# 3. Crear .env
cp .env.example .env
# Editar .env y añadir tu NOTION_TOKEN

# 4. Ejecutar
python generate_dashboard.py
```

## Variables de entorno

| Variable | Descripción |
|---|---|
| `NOTION_TOKEN` | Token de integración de Notion (`secret_...`) |

## Notion Integration

1. Ve a https://www.notion.so/my-integrations
2. Crea una integración llamada `Finance Dashboard`
3. Copia el token al `.env`
4. Conecta la integración a tu Finance Tracker en Notion

## Bases de datos requeridas

| DB | REST API ID |
|---|---|
| Budgets | `39d6673d-a868-4521-9acd-5e5543f4d705` |
| Accounts | `0f4234a1-4ebd-46c2-9ea7-4b2a990b19f7` |

## Tipos de transacción soportados

| Type | Efecto |
|---|---|
| `Income` | Suma a Income |
| `Expense` | Suma a Expenses |
| `Invest` | Suma a Savings (no afecta Net Worth directamente) |
| `Transfer In / Out` | Neutral — no afecta Income/Expenses |
| `Profits` | Solo afecta Net Worth (revalorización de activos) |
