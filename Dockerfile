# Usa un'immagine base di Python (versione slim per un'immagine più leggera)
FROM python:3.9-slim

# Installa le dipendenze di sistema necessarie:
# wget, unzip, curl per scaricare e decomprimere file,
# chromium e chromium-driver per far girare Selenium in modalità headless.
RUN apt-get update && apt-get install -y \
    wget \
    unzip \
    curl \
    chromium \
    chromium-driver \
    && rm -rf /var/lib/apt/lists/*

# Imposta la directory di lavoro all'interno del container
WORKDIR /app

# Aggiorna pip
RUN pip install --upgrade pip

# Copia *prima* il file dei requisiti nella directory di lavoro
# Questo passaggio beneficia della cache di Docker se requirements.txt non cambia
COPY requirements.txt .

# Installa le librerie Python DAL FILE requirements.txt
# Aggiunto --no-cache-dir per evitare potenziali problemi di cache di pip
RUN pip install --no-cache-dir -r requirements.txt

# Aggiungiamo una verifica per essere sicuri (opzionale ma utile per debug)
RUN echo "Pacchetti installati dopo requirements.txt:" && pip freeze

# Copia tutti i file del repository (incluso il codice .py) nella directory di lavoro
COPY . .

# Avvia l'applicazione utilizzando Gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--timeout", "240", "tradetracker_due_mesi_selenium:app"]
