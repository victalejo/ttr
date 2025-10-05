# main.py - Traductor en Tiempo Real con IA - OPTIMIZADO PARA BAJA LATENCIA
import os
import sys
import asyncio
import json
import base64
import sounddevice as sd
import numpy as np
import websockets
import deepl
import webrtcvad
from collections import deque
import config

# Configurar salida UTF-8 para emojis en Windows
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except:
        pass

### ========== CONFIGURACIÓN ==========
SAMPLE_RATE = config.SAMPLE_RATE
CHANNELS = config.CHANNELS
BLOCK_MS = 20  # 20ms para menor latencia (WebRTC VAD óptimo)
BLOCK_SAMPLES = SAMPLE_RATE * BLOCK_MS // 1000

# Nombres de dispositivos
MIC_NAME = config.MICROPHONE_NAME
SPEAKERS_NAME = config.SPEAKERS_NAME
VB_CABLE_INPUT = config.VB_CABLE_INPUT_NAME
VB_CABLE_OUTPUT = config.VB_CABLE_OUTPUT_NAME

# API Keys
DG_KEY = config.DEEPGRAM_API_KEY
DEEPL_KEY = config.DEEPL_API_KEY
ELEVEN_KEY = config.ELEVENLABS_API_KEY
ELEVEN_VOICE_ID = config.ELEVENLABS_VOICE_ID

### ========== UTILIDADES ==========
def find_device(name_hint: str, kind: str):
    """Encuentra dispositivo por nombre. kind: 'input' o 'output'"""
    devices = sd.query_devices()
    for idx, dev in enumerate(devices):
        if name_hint.lower() in dev['name'].lower():
            # Verificar que sea del tipo correcto
            if kind == 'input' and dev['max_input_channels'] > 0:
                # Preferir MME o DirectSound sobre WDM-KS
                hostapi = sd.query_hostapis()[dev['hostapi']]['name']
                if 'WDM-KS' not in hostapi:
                    print(f"✅ Dispositivo {kind}: [{idx}] {dev['name']} ({hostapi})")
                    return idx
            elif kind == 'output' and dev['max_output_channels'] > 0:
                hostapi = sd.query_hostapis()[dev['hostapi']]['name']
                if 'WDM-KS' not in hostapi:
                    print(f"✅ Dispositivo {kind}: [{idx}] {dev['name']} ({hostapi})")
                    return idx
    
    raise RuntimeError(f"❌ No se encontró dispositivo {kind} con nombre: {name_hint}")

### ========== TRADUCCIÓN ASÍNCRONA ==========
translator = deepl.Translator(DEEPL_KEY)

async def translate_text_async(text: str, target: str):
    """Traduce texto de forma asíncrona (no bloquea event loop). target: 'EN-US' o 'ES'"""
    if not text.strip():
        return ""
    try:
        loop = asyncio.get_event_loop()
        # Ejecutar en thread pool para no bloquear
        result = await loop.run_in_executor(
            None,
            lambda: translator.translate_text(text, target_lang=target)
        )
        return result.text
    except Exception as e:
        print(f"❌ Error traduciendo: {e}")
        return ""

### ========== SPEECH-TO-TEXT (Deepgram nova-3 con emisión incremental) ==========
async def deepgram_stt(audio_queue: asyncio.Queue, lang: str, text_queue: asyncio.Queue):
    """
    Streaming STT con Deepgram (nova-3) + endpointing corto + emisión incremental.
    lang: 'es' o 'en'
    """
    # Usar en-US para inglés y es para español
    language = "en-US" if lang.startswith("en") else "es"
    
    # Configuración básica compatible con todos los planes
    uri = (
        "wss://api.deepgram.com/v1/listen"
        f"?language={language}"
        f"&encoding=linear16"
        f"&sample_rate=16000"
        f"&punctuate=true"
        f"&interim_results=true"
    )
    headers = {"Authorization": f"Token {DG_KEY}"}
    
    reconnects = 0
    while True:
        try:
            if reconnects:
                await asyncio.sleep(min(2 + reconnects * 0.5, 5))
            else:
                print(f"🎤 [{language}] Conectando a Deepgram...")
            
            async with websockets.connect(uri, additional_headers=headers, ping_interval=20) as ws:
                print(f"✅ [{language}] Deepgram conectado (nova-3)")
                reconnects = 0
                
                # --- Emisor de audio ---
                async def send_audio():
                    while True:
                        chunk = await audio_queue.get()
                        if chunk is None:
                            break
                        await ws.send(chunk)
                        # Sin sleep artificial - dejamos que WS module backpressure
                    # Si cortamos manualmente, finalizamos
                    try:
                        await ws.send(json.dumps({"type": "Finalize"}))
                    except:
                        pass
                
                # --- Receptor de textos (HÍBRIDO: finales + incrementales con puntuación) ---
                async def receive_text():
                    last_emitted_text = ""   # último texto emitido completo
                    last_emit_t = asyncio.get_event_loop().time()
                    EMIT_TIMEOUT = 1.5  # Emitir cada 1.5s para evitar timeout de ElevenLabs
                    
                    async for msg in ws:
                        try:
                            data = json.loads(msg)
                        except:
                            continue
                        
                        # Mensajes "Results" con transcripción
                        ch = data.get("channel")
                        if not isinstance(ch, dict):
                            continue
                        alts = ch.get("alternatives", [])
                        if not alts:
                            continue
                        
                        transcript = alts[0].get("transcript", "") or ""
                        is_final = data.get("is_final") or data.get("speech_final")
                        
                        now = asyncio.get_event_loop().time()
                        
                        # SOLO emitir cuando es FINAL (evita repeticiones)
                        if is_final:
                            text = transcript.strip()
                            if text:
                                # Verificar si es realmente nuevo contenido
                                # (no está contenido en el último texto ni es idéntico)
                                is_duplicate = (
                                    text == last_emitted_text or
                                    text in last_emitted_text or
                                    last_emitted_text in text and len(text) - len(last_emitted_text) < 3
                                )
                                
                                if not is_duplicate:
                                    print(f"📝 [{language}] ✅ FINAL: {text}")
                                    await text_queue.put(text)
                                    last_emitted_text = text
                                else:
                                    print(f"⏭️ [{language}] Fragmento ignorado (ya emitido): '{text}'")
                        else:
                            # Mostrar progreso pero NO emitir (solo FINALES)
                            if transcript:
                                print(f"� [{language}] Escuchando: {transcript}", end="\r")
                    
                await asyncio.gather(send_audio(), receive_text())
                
        except Exception as e:
            reconnects += 1
            print(f"⚠️ [{language}] Deepgram desconectado: {e}. Reconectando...")
            continue

### ========== TEXT-TO-SPEECH (ElevenLabs WebSocket STREAMING - LATENCIA MÍNIMA) ==========
async def elevenlabs_tts_stream(text_queue: asyncio.Queue, output_device: int, lang_label: str):
    """
    TTS por WebSocket streaming (PCM 16 kHz) para latencia mínima.
    Mantiene la conexión abierta y reproduce a medida que llegan los trozos.
    """
    ws_url = (
        f"wss://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}/stream-input"
        "?model_id=eleven_turbo_v2_5&output_format=pcm_16000"  # PCM 16kHz
    )
    # Usar additional_headers (compatible con versión antigua de websockets)
    headers = {"xi-api-key": ELEVEN_KEY}
    
    # Buffer para deduplicación de texto (evitar enviar repetidos)
    last_sent_text = ""  # Último texto enviado
    
    # Intentar diferentes configuraciones de latencia (de más a menos óptima)
    stream = None
    audio_configs = [
        {"name": "WASAPI exclusivo", "latency": "low", "exclusive": True},
        {"name": "WASAPI compartido", "latency": "low", "exclusive": False},
        {"name": "Latencia baja", "latency": "low", "exclusive": None},
        {"name": "Latencia normal", "latency": None, "exclusive": None},
    ]
    
    for audio_config in audio_configs:
        try:
            # Configurar extra_settings si aplica
            extra = None
            if audio_config["exclusive"] is not None:
                try:
                    from sounddevice import WasapiSettings
                    extra = WasapiSettings(exclusive=audio_config["exclusive"])
                except:
                    continue
            
            # Intentar abrir el stream
            stream_params = {
                "samplerate": 16000,  # Coincidir con formato de ElevenLabs
                "channels": 1,
                "dtype": "int16",
                "device": output_device,
                "blocksize": BLOCK_SAMPLES
            }
            if audio_config["latency"]:
                stream_params["latency"] = audio_config["latency"]
            if extra:
                stream_params["extra_settings"] = extra
            
            stream = sd.RawOutputStream(**stream_params)
            stream.start()
            print(f"🔊 [{lang_label}] TTS WebSocket iniciado ({audio_config['name']})")
            break  # Éxito!
        except Exception as e:
            if audio_config == audio_configs[-1]:  # Último intento
                raise RuntimeError(f"No se pudo iniciar salida de audio: {e}")
            continue  # Intentar siguiente configuración
    
    if not stream:
        raise RuntimeError(f"No se pudo iniciar TTS stream para {lang_label}")
    
    reconnects = 0
    while True:
        try:
            if reconnects:
                await asyncio.sleep(min(2 + reconnects * 0.5, 5))
            
            async with websockets.connect(ws_url, additional_headers=headers, ping_interval=20) as ws:
                print(f"✅ [{lang_label}] ElevenLabs WS conectado")
                reconnects = 0
                
                # Warmup inicial con configuración (según docs de ElevenLabs)
                init = {
                    "text": " ",
                    "voice_settings": {
                        "stability": config.VOICE_STABILITY,
                        "similarity_boost": config.VOICE_SIMILARITY,
                        "use_speaker_boost": False
                    },
                    "generation_config": {
                        "chunk_length_schedule": [50, 120, 160, 250]  # Baja latencia
                    },
                    "xi_api_key": ELEVEN_KEY
                }
                await ws.send(json.dumps(init))
                
                async def sender():
                    nonlocal last_sent_text
                    while True:
                        text = await text_queue.get()
                        if text is None:
                            # Solo salir del loop, NO cerrar WS (keepalive lo mantiene vivo)
                            break
                        
                        # Deduplicación inteligente: detectar duplicados y fragmentos
                        text_clean = text.strip()
                        if text_clean:
                            # Solo bloquear duplicados EXACTOS (menos agresivo)
                            is_duplicate = text_clean.lower() == last_sent_text.lower()
                            
                            if not is_duplicate:
                                last_sent_text = text_clean
                                print(f"🗣️ [{lang_label}] ⚡ Enviando: {text}")
                                # Enviar con flush: true para generar inmediatamente
                                await ws.send(json.dumps({
                                    "text": text,
                                    "flush": True  # Forzar generación inmediata
                                }))
                            else:
                                print(f"⏭️ [{lang_label}] ⏸️ Duplicado exacto omitido: '{text_clean}'")
                
                async def keepalive():
                    """Mantiene la conexión activa enviando espacios cada 15s"""
                    try:
                        while True:
                            await asyncio.sleep(15)  # Cada 15 segundos
                            # Enviar espacio para mantener conexión (según docs)
                            await ws.send(json.dumps({"text": " "}))
                            print(f"💓 [{lang_label}] Keepalive enviado")
                    except asyncio.CancelledError:
                        pass
                
                async def receiver():
                    first_chunk = True
                    chunk_count = 0
                    async for raw in ws:
                        try:
                            data = json.loads(raw)
                        except Exception as e:
                            print(f"⚠️ [{lang_label}] Error parseando respuesta: {e}")
                            continue
                        
                        # Debug: ver qué llega
                        if "audio" not in data and "error" in data:
                            print(f"❌ [{lang_label}] Error de ElevenLabs: {data.get('error')}")
                        
                        audio_b64 = data.get("audio")
                        if audio_b64:
                            chunk_count += 1
                            pcm = base64.b64decode(audio_b64)
                            if first_chunk:
                                print(f"🔊 [{lang_label}] ⚡ PRIMERA SÍLABA reproducida!")
                                first_chunk = False
                            stream.write(pcm)
                            # Debug: mostrar progreso cada 5 chunks
                            if chunk_count % 5 == 0:
                                print(f"🎵 [{lang_label}] Reproduciendo chunk {chunk_count}...")
                        
                        # Verificar si es el último chunk
                        if data.get("isFinal"):
                            print(f"✅ [{lang_label}] Síntesis completada ({chunk_count} chunks)")
                            first_chunk = True  # Resetear para próximo mensaje
                            chunk_count = 0
                
                await asyncio.gather(sender(), receiver(), keepalive())
                
        except Exception as e:
            reconnects += 1
            print(f"⚠️ [{lang_label}] TTS WS desconectado: {e}. Reconectando...")
            if reconnects > 10:
                print(f"❌ [{lang_label}] Demasiados fallos de reconexión. Abortando.")
                break
            continue
        finally:
            # Si salimos del bucle, detener
            if reconnects > 10:
                break
    
    stream.stop()
    stream.close()
    print(f"⏹️ [{lang_label}] TTS detenido")

### ========== CAPTURA DE AUDIO CON VAD ==========
class AudioCapture:
    """Captura audio de un dispositivo y usa VAD para detectar voz"""
    
    def __init__(self, device_idx: int, audio_queue: asyncio.Queue, name: str):
        self.device_idx = device_idx
        self.audio_queue = audio_queue
        self.name = name
        self.vad = webrtcvad.Vad(config.VAD_AGGRESSIVENESS)
        self.buffer = bytearray()
        self.stream = None
        self.is_speaking = False
        self.silence_frames = 0
        self.sent_chunks = 0
        self.loop = None  # Event loop para put_nowait
        self.discarded_chunks = 0  # Contador de bloques descartados
    
    def _safe_put_audio(self, chunk):
        """Intenta agregar audio a la cola; si está llena, descarta audio antiguo"""
        def put_with_discard():
            try:
                # Intentar agregar sin bloquear
                self.audio_queue.put_nowait(chunk)
            except asyncio.QueueFull:
                # Cola llena: descartar un bloque antiguo y agregar el nuevo
                try:
                    self.audio_queue.get_nowait()  # Descartar el más antiguo
                    self.audio_queue.put_nowait(chunk)  # Agregar el nuevo
                    self.discarded_chunks += 1
                    if self.discarded_chunks % 50 == 0:  # Log cada 50 descartes
                        print(f"⚠️ [{self.name}] Cola llena: {self.discarded_chunks} bloques descartados")
                except:
                    pass  # Si falla, simplemente ignorar este bloque
        
        self.loop.call_soon_threadsafe(put_with_discard)
        
    def callback(self, indata, frames, time_info, status):
        """Callback llamado por sounddevice cuando hay audio"""
        if status:
            print(f"⚠️ [{self.name}] Estado: {status}")
        
        # Convertir a bytes
        audio_bytes = bytes(indata)
        self.buffer.extend(audio_bytes)
        
        # Procesar en bloques de 20ms
        while len(self.buffer) >= BLOCK_SAMPLES * 2:
            chunk = bytes(self.buffer[:BLOCK_SAMPLES * 2])
            self.buffer = self.buffer[BLOCK_SAMPLES * 2:]
            
            # Detectar voz con VAD
            try:
                is_speech = self.vad.is_speech(chunk, SAMPLE_RATE)
                
                if is_speech:
                    if not self.is_speaking:
                        print(f"🎙️ [{self.name}] Detectada voz")
                        self.is_speaking = True
                        self.sent_chunks = 0
                    self.silence_frames = 0
                    # Enviar audio a la cola usando el loop correcto
                    if self.loop and self.loop.is_running():
                        self._safe_put_audio(chunk)
                        self.sent_chunks += 1
                        if self.sent_chunks % 100 == 0:  # Log cada 100 bloques (~2s con 20ms)
                            print(f"🎵 [{self.name}] Enviados {self.sent_chunks} bloques de audio a la cola")
                else:
                    if self.is_speaking:
                        self.silence_frames += 1
                        # Enviar algunos frames de silencio después de hablar
                        if self.silence_frames < 30:  # ~600ms de silencio
                            if self.loop and self.loop.is_running():
                                self._safe_put_audio(chunk)
                                self.sent_chunks += 1
                        else:
                            print(f"🔇 [{self.name}] Fin de voz ({self.sent_chunks} bloques enviados)")
                            self.is_speaking = False
                            self.silence_frames = 0
            except Exception as e:
                print(f"❌ [{self.name}] Error VAD: {e}")
    
    def start(self):
        """Inicia la captura de audio con latencia baja"""
        # Intentar diferentes configuraciones de latencia (de más a menos óptima)
        audio_configs = [
            {"name": "WASAPI exclusivo", "latency": "low", "exclusive": True},
            {"name": "WASAPI compartido", "latency": "low", "exclusive": False},
            {"name": "Latencia baja", "latency": "low", "exclusive": None},
            {"name": "Latencia normal", "latency": None, "exclusive": None},
        ]
        
        for audio_config in audio_configs:
            try:
                # Configurar extra_settings si aplica
                extra = None
                if audio_config["exclusive"] is not None:
                    try:
                        from sounddevice import WasapiSettings
                        extra = WasapiSettings(exclusive=audio_config["exclusive"])
                    except:
                        continue
                
                # Intentar abrir el stream
                stream_params = {
                    "samplerate": SAMPLE_RATE,
                    "channels": CHANNELS,
                    "dtype": 'int16',
                    "device": self.device_idx,
                    "blocksize": BLOCK_SAMPLES,
                    "callback": self.callback
                }
                if audio_config["latency"]:
                    stream_params["latency"] = audio_config["latency"]
                if extra:
                    stream_params["extra_settings"] = extra
                
                self.stream = sd.RawInputStream(**stream_params)
                self.stream.start()
                print(f"🎤 [{self.name}] Captura iniciada ({audio_config['name']})")
                return  # Éxito!
            except Exception as e:
                if audio_config == audio_configs[-1]:  # Último intento
                    raise RuntimeError(f"No se pudo iniciar captura de audio: {e}")
                continue  # Intentar siguiente configuración
    
    def stop(self):
        """Detiene la captura de audio"""
        if self.stream:
            self.stream.stop()
            self.stream.close()
            print(f"⏹️ [{self.name}] Captura detenida")

### ========== PIPELINE PRINCIPAL ==========
async def main():
    """Pipeline principal del traductor"""
    
    print("\n" + "="*60)
    print("🌍 TRADUCTOR EN TIEMPO REAL CON IA - OPTIMIZADO")
    print("="*60 + "\n")
    
    # 1. Encontrar dispositivos
    print("🔍 Buscando dispositivos de audio...\n")
    try:
        mic_idx = find_device(MIC_NAME, 'input')
        speakers_idx = find_device(SPEAKERS_NAME, 'output')
        vb_input_idx = find_device(VB_CABLE_INPUT, 'output')
        vb_output_idx = find_device(VB_CABLE_OUTPUT, 'input')
    except Exception as e:
        print(f"\n❌ Error: {e}")
        print("\n💡 Tip: Ejecuta 'python list_devices.py' para ver tus dispositivos")
        return
    
    print("\n" + "="*60)
    print("📋 CONFIGURACIÓN DEL FLUJO:")
    print("="*60)
    print("🎤 Tu micrófono (ES) → Deepgram → DeepL → ElevenLabs → VB-Cable (EN) → Reunión")
    print("🎧 Reunión (EN) → VB-Cable → Deepgram → DeepL → ElevenLabs → Tus auriculares (ES)")
    print("="*60 + "\n")
    
    # 2. Crear colas de comunicación
    # TU VOZ: Español → Inglés
    mic_audio_q = asyncio.Queue(maxsize=500)
    es_text_q = asyncio.Queue(maxsize=50)
    en_tts_text_q = asyncio.Queue(maxsize=50)
    
    # VOZ DE OTROS: Inglés → Español
    meeting_audio_q = asyncio.Queue(maxsize=500)
    en_text_q = asyncio.Queue(maxsize=50)
    es_tts_text_q = asyncio.Queue(maxsize=50)
    
    # Timestamps para cancelación de eco
    last_en_synthesis = {'time': 0}
    last_es_synthesis = {'time': 0}
    
    # 3. Iniciar captura de audio
    mic_capture = AudioCapture(mic_idx, mic_audio_q, "TU VOZ")
    meeting_capture = AudioCapture(vb_output_idx, meeting_audio_q, "REUNIÓN")
    
    # Asignar el event loop actual a las capturas
    loop = asyncio.get_event_loop()
    mic_capture.loop = loop
    meeting_capture.loop = loop
    
    mic_capture.start()
    meeting_capture.start()
    
    # 4. Traductor ES→EN (tu voz)
    async def translate_es_to_en():
        print("🔄 Traductor ES→EN iniciado")
        while True:
            text_es = await es_text_q.get()
            if text_es is None:
                await en_tts_text_q.put(None)
                break
            
            print(f"🔄 Traduciendo ES→EN: '{text_es}'")
            text_en = await translate_text_async(text_es, "EN-US")
            if text_en:
                print(f"✅ 🇪🇸→🇬🇧 '{text_es}' → '{text_en}'")
                # Marcar timestamp de síntesis en inglés
                last_en_synthesis['time'] = asyncio.get_event_loop().time()
                await en_tts_text_q.put(text_en)
            else:
                print(f"⚠️ No se pudo traducir: '{text_es}'")
    
    # 5. Traductor EN→ES (voz de otros)
    async def translate_en_to_es():
        print("🔄 Traductor EN→ES iniciado")
        while True:
            text_en = await en_text_q.get()
            if text_en is None:
                await es_tts_text_q.put(None)
                break
            
            # Verificar si este audio es eco del sistema
            current_time = asyncio.get_event_loop().time()
            time_since_synthesis = current_time - last_en_synthesis['time']
            
            if time_since_synthesis < 8.0:
                print(f"🔇 Ignorando eco del sistema (EN): '{text_en}' ({time_since_synthesis:.1f}s desde síntesis)")
                continue
            
            print(f"🔄 Traduciendo EN→ES: '{text_en}'")
            text_es = await translate_text_async(text_en, "ES")
            if text_es:
                print(f"✅ 🇬🇧→🇪🇸 '{text_en}' → '{text_es}'")
                # Marcar timestamp de síntesis en español
                last_es_synthesis['time'] = current_time
                await es_tts_text_q.put(text_es)
            else:
                print(f"⚠️ No se pudo traducir: '{text_en}'")
    
    # 6. Lanzar todas las tareas
    print("🚀 Iniciando pipeline...\n")
    
    tasks = [
        # STT con nova-3 y emisión incremental
        asyncio.create_task(deepgram_stt(mic_audio_q, "es", es_text_q)),
        asyncio.create_task(deepgram_stt(meeting_audio_q, "en", en_text_q)),
        
        # Traducción asíncrona
        asyncio.create_task(translate_es_to_en()),
        asyncio.create_task(translate_en_to_es()),
        
        # TTS streaming WebSocket
        asyncio.create_task(elevenlabs_tts_stream(en_tts_text_q, vb_input_idx, "EN→REUNIÓN")),
        asyncio.create_task(elevenlabs_tts_stream(es_tts_text_q, speakers_idx, "ES→TÚ")),
    ]
    
    print("✅ Sistema activo. Habla por tu micrófono!\n")
    print("💡 Presiona Ctrl+C para detener\n")
    print("="*60 + "\n")
    
    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        print("\n\n⏹️ Deteniendo...")
    finally:
        mic_capture.stop()
        meeting_capture.stop()
        print("✅ Sistema detenido")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 ¡Hasta luego!")
