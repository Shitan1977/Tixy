import requests
from bs4 import BeautifulSoup
import json


# Funzione per ottenere i dati di eventi da TicketOne
def get_ticketone_events(url="https://www.ticketone.it/"):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }

    try:
        # Fai la richiesta HTTP per ottenere la pagina con un timeout di 5 secondi
        response = requests.get(url, headers=headers, timeout=5)  # Aggiunto header
    except requests.exceptions.Timeout:
        print("La richiesta ha superato il tempo limite.")
        return []

    # Se la richiesta è stata completata correttamente
    if response.status_code == 200:
        # Usa BeautifulSoup per analizzare la pagina HTML
        soup = BeautifulSoup(response.text, "html.parser")

        # Trova tutti gli eventi nella pagina
        events = []

        # Trova tutte le carte di eventi
        event_cards = soup.find_all("div", class_="swiper-slide")  # Nuovo selettore per i "swiper-slide"

        # Limitiamo l'estrazione a 2 eventi
        for i, event in enumerate(event_cards[:2]):  # solo i primi 2 eventi
            # Estrai nome evento, data, luogo, link URL
            name = event.find("div", class_="editorial-swiper-title").text.strip() if event.find("div",
                                                                                                 class_="editorial-swiper-title") else "Evento Sconosciuto"
            subtitle = event.find("div", class_="editorial-swiper-subtitle").text.strip() if event.find("div",
                                                                                                        class_="editorial-swiper-subtitle") else "Sottotitolo sconosciuto"
            link = event.find("a", href=True)['href'] if event.find("a", href=True) else "URL non disponibile"
            image_url = event.find("img")['src'] if event.find("img") else "Immagine non disponibile"

            # Salva i dettagli dell'evento
            events.append({
                "name": name,
                "subtitle": subtitle,
                "url": "https://www.ticketone.it" + link,  # Assicurati di avere il link completo
                "image_url": image_url,
            })

        return events
    else:
        print("Errore nella richiesta HTTP")
        return []


# Esegui lo scraper
events = get_ticketone_events()

# Visualizza i risultati (solo i primi 2 eventi)
print(json.dumps(events, indent=2, ensure_ascii=False))
