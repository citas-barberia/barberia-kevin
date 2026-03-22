from flask import Flask, render_template, request, redirect, flash, url_for, jsonify, make_response
from datetime import date, timedelta, datetime
from zoneinfo import ZoneInfo
import os
import uuid
import requests
import time
import urllib.parse

TZ = ZoneInfo(os.getenv("TZ", "America/Costa_Rica"))
app = Flask(__name__)
app.secret_key = "secret_key"

# =========================
# CONFIG WHATSAPP (Meta)
# =========================
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "barberia123")
NUMERO_BARBERO = os.getenv("NUMERO_BARBERO", "50672314147")
DOMINIO = os.getenv("DOMINIO", "https://barberia-kevin.onrender.com")
NOMBRE_BARBERO = os.getenv("NOMBRE_BARBERO", "Kevin")
CLAVE_BARBERO = os.getenv("CLAVE_BARBERO", "1234")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")

PROCESADOS = {}
TTL_MSG = 60 * 10 

# =========================
# Helpers
# =========================
def normalizar_barbero(barbero: str) -> str:
    if not barbero: return ""
    barbero = " ".join(barbero.strip().split())
    return barbero.title()

def enviar_whatsapp(to_numero: str, mensaje: str) -> bool:
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        print("⚠️ Faltan WHATSAPP_TOKEN o PHONE_NUMBER_ID")
        return False
    to_numero = str(to_numero).replace("+", "").replace(" ", "").strip()
    url = f"https://graph.facebook.com/v22.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    data = {"messaging_product": "whatsapp", "to": to_numero, "type": "text", "text": {"body": mensaje}}
    try:
        r = requests.post(url, headers=headers, json=data, timeout=15)
        print("📤 WhatsApp -> to:", to_numero, "| status:", r.status_code)
        return r.status_code < 400
    except Exception as e:
        print("❌ Error enviando WhatsApp:", e)
        return False

def es_numero_whatsapp(valor: str) -> bool:
    if not valor: return False
    s = str(valor).strip()
    return s.isdigit() and len(s) >= 8

def barbero_autenticado() -> bool:
    return request.cookies.get("clave_barbero") == CLAVE_BARBERO

def _precio_a_int(valor):
    if valor is None: return 0
    s = str(valor).replace("₡", "").replace(",", "").strip()
    try: return int(float(s))
    except: return 0

def _hora_ampm_a_time(hora_str: str):
    if not hora_str: return None
    # Limpiamos el texto: quitamos espacios y lo pasamos a minúsculas
    s = str(hora_str).strip().lower().replace(" ", "")
    
    # Intentamos formato 12h (08:00am, 1:30pm)
    try:
        if "am" in s or "pm" in s:
            return datetime.strptime(s, "%I:%M%p").time()
    except: pass

    # Intentamos formato 24h (08:00, 13:30)
    try:
        return datetime.strptime(s, "%H:%M").time()
    except: pass

    # Si llega con segundos (algunas DB lo mandan así: 13:30:00)
    try:
        return datetime.strptime(s, "%H:%M:%S").time()
    except: pass

    return None
def _now_cr():
    return datetime.now(TZ)

# =========================
# Servicios y horas
# =========================
servicios = {
    "Corte sencillo": 4500,
    "Corte y Barba": 6000,
    "Corte y lavado": 5500,
    "Cejas": 2500,
    "Barba": 2500,
    
}

# =========================
# SUPABASE & TXT Logic
# =========================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
USAR_SUPABASE = bool(SUPABASE_URL and SUPABASE_KEY)
SUPABASE_TIMEOUT = int(os.getenv("SUPABASE_TIMEOUT", "10"))

def _supabase_headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json", "Accept": "application/json"}

def _supabase_request(method, url, params=None, json_body=None, extra_headers=None):
    headers = _supabase_headers()
    if extra_headers: headers.update(extra_headers)
    try:
        r = requests.request(method=method, url=url, params=params, json=json_body, headers=headers, timeout=SUPABASE_TIMEOUT)
        r.raise_for_status()
        return r.json() if r.text else None
    except Exception as e:
        print(f"⚠️ Supabase falló:", e)
        return None

def leer_citas_txt():
    citas = []
    try:
        with open("citas.txt", "r", encoding="utf-8") as f:
            for linea in f:
                if not linea.strip(): continue
                c = linea.strip().split("|")
                if len(c) >= 8:
                    dur = c[8] if len(c) == 9 else "30"
                    citas.append({
                        "id": c[0], "cliente": c[1], "cliente_id": c[2], 
                        "barbero": c[3], "servicio": c[4], "precio": c[5], 
                        "fecha": c[6], "hora": c[7], "duracion": dur
                    })
    except FileNotFoundError: pass
    return citas

def guardar_cita_txt(id_cita, cliente, cliente_id, barbero, servicio, precio, fecha, hora, duracion):
    with open("citas.txt", "a", encoding="utf-8") as f:
        f.write(f"{id_cita}|{cliente}|{cliente_id}|{barbero}|{servicio}|{precio}|{fecha}|{hora}|{duracion}\n")

def leer_citas_db():
    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/citas"
    # AGREGAMOS ESTO: que solo traiga las citas donde el barbero sea Kevin
    params = {
        "select": "*",
        "barbero": "eq.Kevin",  # <--- ESTA LÍNEA ES EL FILTRO MAESTRO
        "order": "fecha.asc,hora.asc"
    }
    data = _supabase_request("GET", url, params=params)
    # ... resto del código igual ...
    if data is None: return None
    
    citas_procesadas = []
    dias = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    for r in data:
        hora_original = str(r.get("hora", ""))
        try:
            hora_obj = datetime.strptime(hora_original, "%H:%M")
            hora_bonita = hora_obj.strftime("%I:%M %p").lower()
        except:
            hora_bonita = hora_original

        fecha_raw = str(r.get("fecha", ""))
        try:
            fecha_obj = datetime.strptime(fecha_raw, "%Y-%m-%d")
            fecha_bonita = f"{dias[fecha_obj.weekday()]} {fecha_obj.strftime('%d/%m/%Y')}"
        except:
            fecha_bonita = fecha_raw

        citas_procesadas.append({
            "id": r.get("id"), "cliente": r.get("cliente", ""), 
            "cliente_id": r.get("cliente_id", ""), "barbero": r.get("barbero", ""), 
            "servicio": r.get("servicio", ""), "precio": str(r.get("precio", "")), 
            "fecha": fecha_bonita, "hora": hora_bonita,
            "duracion": str(r.get("duracion", "30"))
        })
    return citas_procesadas

def guardar_cita_db(cliente, cliente_id, barbero, servicio, precio, fecha, hora, duracion):
    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/citas"
    body = {
        "cliente": cliente, "cliente_id": str(cliente_id), "barbero": barbero, 
        "servicio": servicio, "precio": int(precio), "fecha": fecha, 
        "hora": hora, "duracion": int(duracion)
    }
    res = _supabase_request("POST", url, json_body=body, extra_headers={"Prefer": "return=minimal"})
    return res is not None

def leer_citas():
    # Siempre intentamos leer de DB primero para tener el orden correcto
    if USAR_SUPABASE:
        data = leer_citas_db()
        if data is not None: return data
    return leer_citas_txt()

def guardar_cita(id_cita, cliente, cliente_id, barbero, servicio, precio, fecha, hora, duracion):
    if USAR_SUPABASE:
        try:
            if not guardar_cita_db(cliente, cliente_id, barbero, servicio, precio, fecha, hora, duracion):
                guardar_cita_txt(id_cita, cliente, cliente_id, barbero, servicio, precio, fecha, hora, duracion)
        except: 
            guardar_cita_txt(id_cita, cliente, cliente_id, barbero, servicio, precio, fecha, hora, duracion)
    else: 
        guardar_cita_txt(id_cita, cliente, cliente_id, barbero, servicio, precio, fecha, hora, duracion)

def _reescribir_citas_txt_actualizando_servicio(id_cita, nuevo_servicio):
    citas = leer_citas_txt()
    with open("citas.txt", "w", encoding="utf-8") as f:
        for c in citas:
            cid = c.get("id")
            srv = nuevo_servicio if str(cid) == str(id_cita) else c['servicio']
            f.write(f"{cid}|{c['cliente']}|{c['cliente_id']}|{c['barbero']}|{srv}|{c['precio']}|{c['fecha']}|{c['hora']}|{c.get('duracion','30')}\n")

def cancelar_cita_por_id(id_cita):
    if USAR_SUPABASE:
        url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/citas"
        _supabase_request("PATCH", url, params={"id": f"eq.{id_cita}"}, json_body={"servicio": "CITA CANCELADA"})
    _reescribir_citas_txt_actualizando_servicio(id_cita, "CITA CANCELADA")

def marcar_atendida_por_id(id_cita):
    if USAR_SUPABASE:
        url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/citas"
        _supabase_request("PATCH", url, params={"id": f"eq.{id_cita}"}, json_body={"servicio": "CITA ATENDIDA"})
    _reescribir_citas_txt_actualizando_servicio(id_cita, "CITA ATENDIDA")

def buscar_cita_por_id(id_cita):
    for c in leer_citas():
        if str(c.get("id")) == str(id_cita): return c
    return None

# =========================
# RUTAS
# =========================
@app.route("/", methods=["GET", "POST"])
def index():
    # 1. Agarrar el ID del cliente (URL o Cookie)
    cliente_id_url = request.args.get("cliente_id")
    cliente_id_cookie = request.cookies.get("cliente_id")
    cliente_id = cliente_id_url or cliente_id_cookie or str(uuid.uuid4())

    # LIMPIEZA: ID sin 506 para que el link de WhatsApp siempre funcione
    id_busqueda = str(cliente_id).replace("506", "") if str(cliente_id).startswith("506") else str(cliente_id)

    if request.method == "POST":
        try:
            cliente = request.form.get("cliente", "").strip()
            tel_raw = request.form.get("telefono_cliente", "").strip()
            servicio = request.form.get("servicio", "").strip()
            fecha = request.form.get("fecha", "").strip()
            hora_original = request.form.get("hora", "").strip()

            if not cliente or not servicio or not hora_original:
                flash("Por favor rellene todos los campos.")
                return redirect(url_for("index", cliente_id=cliente_id))

            # Guardamos el ID limpio (8 dígitos) para que coincida con el link
            cliente_id_db = tel_raw if len(tel_raw) == 8 else tel_raw.replace("506", "")
            telefono_full = "506" + cliente_id_db
            
            # Formato hora para Supabase
            dt_h = datetime.strptime(hora_original, "%I:%M %p")
            hora_db = dt_h.strftime("%H:%M:00")
            
            duracion = 60 if "BARBA" in servicio.upper() else 30
            precio = servicios.get(servicio, 0)
            id_cita = str(uuid.uuid4())

            # GUARDAR
            guardar_cita(id_cita, cliente, cliente_id_db, NOMBRE_BARBERO, servicio, precio, fecha, hora_db, duracion)

            # Link de gestión para el cliente
            link_gestion = f"{DOMINIO}/?cliente_id={cliente_id_db}"
            msg_c = f"✅ *¡Cita Confirmada!* 💈\n\nHola *{cliente}*, tu espacio para *{servicio}* el {fecha} a las {hora_original} está reservado.\n\nPara gestionar o cancelar presiona aquí:\n{link_gestion}"
            
            # Enviar WhatsApps
            enviar_whatsapp(telefono_full, msg_c)
            enviar_whatsapp(NUMERO_BARBERO, f"💈 Nueva cita (Kevin): {cliente}\n{servicio}\n{fecha} {hora_original}")

            # Pantalla de confirmación
            link_wa_directo = f"https://wa.me/{telefono_full}?text={urllib.parse.quote(msg_c)}"
            return render_template("confirmacion.html", link_wa=link_wa_directo, cliente=cliente)
            
        except Exception as e:
            print(f"Error POST Kevin: {e}")
            flash("Error al procesar la cita.")

    # 2. Filtrar citas para mostrar el botón de CANCELAR
    citas_todas = leer_citas()
    citas_cliente = [
        c for c in citas_todas 
        if (str(c.get("cliente_id")) == id_busqueda or str(c.get("cliente_id")) == str(cliente_id))
        and c.get("servicio") not in ["CITA CANCELADA", "CITA ATENDIDA"]
    ]

    resp = make_response(render_template(
        "index.html", 
        servicios=servicios, 
        citas=citas_cliente, 
        cliente_id=cliente_id,
        nombre_barbero=NOMBRE_BARBERO,
        numero_barbero=NUMERO_BARBERO,
        hoy_iso=_now_cr().strftime("%Y-%m-%d")
    ))
    
    resp.set_cookie("cliente_id", cliente_id, max_age=60*60*24*30)
    return resp

@app.route("/horas")
def horas():
    try:
        fecha_str = request.args.get('fecha')
        if not fecha_str: return jsonify([])
        
        f_obj = datetime.strptime(fecha_str, "%Y-%m-%d").date()
        ahora_cr = datetime.now(TZ).replace(tzinfo=None)
        hoy_cr = ahora_cr.date()

        # 1. Si es ayer, no hay nada que hacer
        if f_obj < hoy_cr:
            return jsonify([])

        # 2. HORARIO DOMINGO (Forzado de 9am a 7pm para que salgan horas SI O SI)
        # 2. HORARIO DE KEVIN (Igual a Junior)
        dia_semana = f_obj.weekday()
        
        if dia_semana == 6: # DOMINGO
            h_i, h_f = 9, 16  # 9am a 4pm
        elif dia_semana in [4, 5]: # VIERNES Y SÁBADO
            h_i, h_f = 8, 20  # 8am a 8pm
        else: # LUNES A JUEVES
            h_i, h_f = 9, 20  # 9am a 8pm

        # 3. Generar horas cada 30 min
        horas_base = []
        temp = datetime.combine(f_obj, datetime.min.time()).replace(hour=h_i)
        fin = datetime.combine(f_obj, datetime.min.time()).replace(hour=h_f)
        while temp < fin:
            horas_base.append(temp.strftime("%H:%M:00"))
            temp += timedelta(minutes=30)

        # 4. Bloqueo de Citas (Try por si falla la red)
        ocupadas = set()
        try:
            citas = leer_citas_fuerza_bruta()
            if citas:
                for c in citas:
                    if str(c.get("fecha")) == fecha_str and "CANCELADA" not in str(c.get("servicio")).upper():
                        h_db = str(c.get("hora"))
                        ocupadas.add(h_db)
                        # Bloqueo de 1 hora para servicios largos
                        if "BARBA" in str(c.get("servicio")).upper() or int(c.get("duracion", 30)) > 30:
                            dt_h = datetime.strptime(h_db, "%H:%M:%S") if ":" in h_db else datetime.strptime(h_db, "%H:%M")
                            ocupadas.add((dt_h + timedelta(minutes=30)).strftime("%H:%M:%S"))
        except:
            print("⚠️ Error leyendo Supabase, mostrando horas libres igual.")

        # 5. EL FILTRO (Con colchón de 30 min)
        res = []
        for h in horas_base:
            h_dt = datetime.strptime(h, "%H:%M:%S")
            # Si hoy a las (hora de la cita) es mayor a (ahora + 30 min)
            if datetime.combine(f_obj, h_dt.time()) > (ahora_cr + timedelta(minutes=30)):
                if h not in ocupadas:
                    res.append(h_dt.strftime("%I:%M %p").upper().lstrip('0'))
                    
        return jsonify(res)

    except Exception as e:
        print(f"❌ ERROR CRÍTICO: {e}")
        return jsonify([])

@app.route("/cancelar", methods=["POST"])
def cancelar():
    id_cita = request.form.get("id")
    cita = buscar_cita_por_id(id_cita)
    
    if not cita: 
        return redirect(url_for("index"))

    # Paso 1: Cancelar la cita de una vez (esto es rápido)
    cancelar_cita_por_id(id_cita)
    
    # Paso 2: El envío de WhatsApp suele ser lento. 
    # Lo metemos en un try para que si falla el servicio, no bloquee la pantalla de Junior.
    try:
        enviar_whatsapp(NUMERO_BARBERO, f"❌ Cita CANCELADA: {cita.get('cliente')} el {cita.get('fecha')} a las {cita.get('hora')}")
    except Exception as e:
        print(f"Error enviando WhatsApp: {e}") # Solo lo logueamos, no detenemos el proceso.

    flash("Cita cancelada correctamente")
    
    # Paso 3: Redirigir de inmediato para que la página cargue de nuevo.
    if barbero_autenticado(): 
        return redirect(url_for("barbero"))
    
    cliente_id = str(cita.get("cliente_id", ""))
    return redirect(url_for("index", cliente_id=cliente_id))

@app.route("/atendida", methods=["POST"])
def atendida():
    id_cita = request.form.get("id")
    if barbero_autenticado() and id_cita:
        marcar_atendida_por_id(id_cita)
        flash("¡Cita completada!") 
    
    # Redirigir de inmediato para que Kevin no espere
    return redirect(url_for("barbero"))

@app.route("/barbero", methods=["GET"])
def barbero():
    clave = request.args.get("clave")
    if barbero_autenticado() or clave == CLAVE_BARBERO:
        # 1. Traemos las citas (Asegúrate que leer_citas() traiga fecha_iso)
        citas = leer_citas()
        hoy_iso = _now_cr().strftime("%Y-%m-%d")
        
        # 2. FILTRAR SOLO LAS DE HOY PARA LOS STATS
        # Esto hace que el conteo sea real y rápido
        citas_hoy = [c for c in citas if str(c.get("fecha_iso")) == hoy_iso or hoy_iso in str(c.get("fecha"))]
        
        # 3. CÁLCULOS (Aquí es donde se arregla lo de cobrar 0)
        atendidas_hoy = [c for c in citas_hoy if c.get("servicio") == "CITA ATENDIDA"]
        canceladas_hoy = [c for c in citas_hoy if c.get("servicio") == "CITA CANCELADA"]
        activas_hoy = [c for c in citas_hoy if c.get("servicio") not in ["CITA CANCELADA", "CITA ATENDIDA"]]

        total_dinero = 0
        for c in atendidas_hoy:
            total_dinero += _precio_a_int(c.get("precio"))

        stats = {
            "cant_total": len(citas_hoy),
            "cant_activas": len(activas_hoy),
            "cant_atendidas": len(atendidas_hoy),
            "cant_canceladas": len(canceladas_hoy),
            "total_cobrado": total_dinero,
            "nombre": NOMBRE_BARBERO
        }
        
        resp = make_response(render_template("barbero.html", citas=citas, stats=stats))
        resp.set_cookie("clave_barbero", CLAVE_BARBERO, max_age=60*60*24*7)
        return resp
        
    return "🔒 Panel protegido."

@app.route("/citas_json")
def citas_json():
    return jsonify({"citas": leer_citas()})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


