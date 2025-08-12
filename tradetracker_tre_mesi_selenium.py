import os
import time
import logging
import calendar
from datetime import datetime
from typing import Optional

from dateutil.relativedelta import relativedelta
from flask import Flask, request
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from google.cloud import storage

# ---------------------------------------------------------------------------
# Configurazione Flask e logging
# ---------------------------------------------------------------------------
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------
def upload_to_gcs(bucket_name: str,
                  source_file_name: str,
                  destination_blob_name: str) -> str:
    """Carica un file su Google Cloud Storage e restituisce il nome del blob."""
    logger.info(f"Upload: {source_file_name} → gs://{bucket_name}/{destination_blob_name}")
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    bucket.blob(destination_blob_name).upload_from_filename(source_file_name)
    return destination_blob_name

# ---------------------------------------------------------------------------
# Core Selenium
# ---------------------------------------------------------------------------
def run_selenium_script(username: str,
                        password: str,
                        folder_id: Optional[str],
                        bucket_name: str) -> Optional[str]:
    """
    • Login TradeTracker, seleziona intervallo di due mesi fa, esporta CSV.
    • Rinomina in <mese_anno>.csv e carica nel bucket:
        - se folder_id valido ⇒ <folder_id>/<nome_file>
        - altrimenti        ⇒ <nome_file> (root del bucket)
    • Ritorna il nome del file o None su errore (salvando screenshot).
    """
    prefix = f"{folder_id.strip()}/" if folder_id and folder_id.strip() else ""
    logger.info(f"Start Selenium — bucket={bucket_name}, prefix='{prefix}'")
    driver = None
    download_dir = "/tmp"
    error_prefix = prefix or "errors/"

    try:
        # --- Chrome headless --------------------------------------------------
        opts = Options()
        opts.add_argument("--headless")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--window-size=1920,1080")
        opts.add_experimental_option("prefs", {
            "download.default_directory": download_dir,
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
        })
        driver = webdriver.Chrome(options=opts)

        # --- LOGIN ------------------------------------------------------------
        driver.get("https://merchant.tradetracker.com/user/login")
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.XPATH, "//*[@id='username']"))
        ).send_keys(username)
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.XPATH, "//*[@id='password']"))
        ).send_keys(password)
        WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.XPATH, "//*[@id='submitLogin']"))
        ).click()

        # --- DASHBOARD LOADED --------------------------------------------------
        WebDriverWait(driver, 20).until(
            EC.visibility_of_element_located((By.XPATH, "//body"))
        )

        # --- PAGINA VENDITE ----------------------------------------------------
        driver.get("https://merchant.tradetracker.com/affiliateTransaction/sales")
        from_date_xpath = "//*[@id='fromDate']"
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.XPATH, from_date_xpath))
        )

        # --- CALCOLO DATE (due mesi fa) ---------------------------------------
        target_month = datetime.now() - relativedelta(months=3)
        start = target_month.replace(day=1)
        end = target_month.replace(
            day=calendar.monthrange(target_month.year, target_month.month)[1]
        )
        start_str, end_str = start.strftime("%d/%m/%Y"), end.strftime("%d/%m/%Y")

        # Set date fields (max 3 tentativi)
        def set_date(xp, value):
            field = driver.find_element(By.XPATH, xp)
            for _ in range(3):
                field.clear(); time.sleep(0.2)
                field.send_keys(value, Keys.ENTER); time.sleep(0.8)
                if field.get_attribute("value").strip() == value:
                    return
            raise ValueError(f"Impossibile impostare data {value}")

        set_date(from_date_xpath, start_str)
        set_date("//*[@id='toDate']", end_str)

        # Applica filtro
        WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.XPATH,
              "//input[@type='submit' and @name='submit_period_p' and @value='OK']"))
        ).click()
        time.sleep(15)

        # --- EXPORT CSV --------------------------------------------------------
        WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.XPATH, "//*[@id='listview-10-export-csv']"))
        ).click()

        # Attendi download (max 90s)
        def wait_csv(dir_, timeout=90):
            t_end = time.time() + timeout
            while time.time() < t_end:
                csvs = [os.path.join(dir_, f) for f in os.listdir(dir_) if f.endswith(".csv")]
                if csvs:
                    latest = max(csvs, key=os.path.getctime)
                    if not latest.endswith(".crdownload"):
                        return latest
                time.sleep(1)
        csv_path = wait_csv(download_dir) or (_ for _ in ()).throw(Exception("CSV timeout"))

        # Rinomina: <mese>_<anno>.csv
        it_months = ["gennaio","febbraio","marzo","aprile","maggio","giugno",
                     "luglio","agosto","settembre","ottobre","novembre","dicembre"]
        new_fname = f"{it_months[target_month.month-1]}_{target_month.year}.csv"
        new_path = os.path.join(download_dir, new_fname)
        os.rename(csv_path, new_path)

        upload_to_gcs(bucket_name, new_path, f"{prefix}{new_fname}")
        return new_fname

    except Exception as exc:
        logger.error(f"Errore Selenium: {exc!r}", exc_info=True)
        if driver:
            try:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                shot = os.path.join(download_dir, f"error_{ts}.png")
                driver.save_screenshot(shot)
                upload_to_gcs(bucket_name, shot, f"{error_prefix}error_{ts}.png")
            except Exception as scr_exc:
                logger.error(f"Screenshot non salvato: {scr_exc!r}")
        return None

    finally:
        if driver:
            driver.quit()

# ---------------------------------------------------------------------------
# HTTP Endpoint
# ---------------------------------------------------------------------------
@app.route("/run-selenium", methods=["GET"])
def call_selenium():
    """
    /run-selenium?username=…&password=…[&folder_id=…][&bucket=…]
      - username, password obbligatori
      - folder_id facoltativo → se omesso carica alla radice del bucket
      - bucket  facoltativo → DEFAULT_BUCKET o 'tradetracker_selenium'
    """
    username = request.args.get("username")
    password = request.args.get("password")
    folder_id = request.args.get("folder_id")          # ora opzionale
    bucket = request.args.get("bucket") or os.getenv("DEFAULT_BUCKET", "tradetracker_selenium")

    if not username or not password:
        return "'username' e 'password' sono obbligatori", 400

    result = run_selenium_script(username, password, folder_id, bucket)
    return (result, 200) if result else ("Errore – vedi log", 500)

# ---------------------------------------------------------------------------
# Avvio locale
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=True)
