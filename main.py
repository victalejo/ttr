# main_fixed.py - Traductor en Tiempo Real con IA
import os, asyncio, json, base64
import sounddevice as sd
import numpy as np
import websockets
import deepl
import webrtcvad
from collections import deque
import config  # Importar configuración
import requests  # Para ElevenLabs REST API

### ========== CONFIGURACIÓN ==========
SAMPLE_RATE = config.SAMPLE_RATE
CHANNELS = config.CHANNELS
BLOCK_MS = config.BLOCK_SIZE_MS
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

### ========== TRADUCCIÓN ==========
translator = deepl.Translator(DEEPL_KEY)

def translate_text(text: str, target: str):
    """Traduce texto. target: 'EN-US' o 'ES'"""
    if not text.strip():
        return ""
    try:
        result = translator.translate_text(text, target_lang=target)
        return result.text
    except Exception as e:
        print(f"❌ Error traduciendo: {e}")
        return ""

### ========== SPEECH-TO-TEXT (Deepgram) ==========
async def deepgram_stt(audio_queue: asyncio.Queue, lang: str, text_queue: asyncio.Queue):
    """
    Conecta a Deepgram WebSocket para transcribir audio con reconexión automática.
    lang: 'es' o 'en'
    """
    uri = (
        f"wss://api.deepgram.com/v1/listen"
        f"?model=nova-2&language={lang}"
        f"&encoding=linear16&sample_rate={SAMPLE_RATE}"
        f"&channels={CHANNELS}&punctuate=true"
        f"&interim_results=true&endpointing=1200"
        f"&utterance_end_ms=2500&vad_events=true"
    )
    headers = {"Authorization": f"Token {DG_KEY}"}
    
    reconnect_count = 0
    max_reconnects = 999  # Reconexiones infinitas
    
    while reconnect_count < max_reconnects:
        try:
            if reconnect_count > 0:
                print(f"🔄 [{lang.upper()}] Reconectando a Deepgram (intento {reconnect_count})...")
                await asyncio.sleep(2)  # Esperar antes de reconectar
            else:
                print(f"🎤 [{lang.upper()}] Conectando a Deepgram...")
            
            async with websockets.connect(uri, additional_headers=headers, ping_interval=20) as ws:
                print(f"✅ [{lang.upper()}] Deepgram conectado")
                reconnect_count = 0  # Resetear contador al conectar exitosamente
                
                async def send_audio():
                    """Envía audio desde la cola al WebSocket"""
                    try:
                        sent_count = 0
                        while True:
                            chunk = await audio_queue.get()
                            if chunk is None:
                                break
                            await ws.send(chunk)
                            sent_count += 1
                            if sent_count % 200 == 0:  # Reducido: log cada 200 bloques (~6 segundos)
                                print(f"🎵 [{lang.upper()}] Enviados {sent_count} bloques a Deepgram...")
                            await asyncio.sleep(0.001)  # Pequeña pausa
                    except Exception as e:
                        print(f"⚠️ [{lang.upper()}] Conexión cerrada (audio): {e}")
                    finally:
                        try:
                            await ws.send(json.dumps({"type": "CloseStream"}))
                        except:
                            pass
                
                async def receive_text():
                    """Recibe transcripciones del WebSocket"""
                    last_transcript = ""
                    last_update_time = asyncio.get_event_loop().time()
                    silence_threshold = 2.0  # 2 segundos de silencio antes de enviar
                    last_sent_transcript = ""  # Última transcripción enviada para evitar duplicados
                    
                    async def check_silence():
                        """Verifica si hay suficiente silencio para enviar la transcripción"""
                        nonlocal last_transcript, last_update_time, last_sent_transcript
                        while True:
                            await asyncio.sleep(0.3)  # Verificar cada 0.3s
                            current_time = asyncio.get_event_loop().time()
                            time_since_update = current_time - last_update_time
                            
                            if last_transcript and time_since_update >= silence_threshold:
                                # Han pasado 2 segundos sin actualización, enviar
                                if last_transcript != last_sent_transcript:  # Solo si es diferente
                                    print(f"📝 [{lang.upper()}] ✅ COMPLETA: {last_transcript}")
                                    try:
                                        await text_queue.put(last_transcript)
                                        last_sent_transcript = last_transcript  # Marcar como enviada
                                    except:
                                        pass
                                # Limpiar para la siguiente transcripción
                                last_transcript = ""
                                last_update_time = current_time
                    
                    # Iniciar tarea de verificación de silencio
                    silence_task = asyncio.create_task(check_silence())
                    
                    try:
                        async for msg in ws:
                            try:
                                data = json.loads(msg)
                                # Deepgram puede enviar varios tipos de mensajes
                                if "channel" in data:
                                    channel = data.get("channel")
                                    if isinstance(channel, dict):
                                        alternatives = channel.get("alternatives", [])
                                        if alternatives and len(alternatives) > 0:
                                            transcript = alternatives[0].get("transcript", "")
                                            
                                            # Limpiar duplicados: "Hello. Hello. Hello." → "Hello."
                                            if transcript.strip():
                                                # Dividir en frases
                                                sentences = [s.strip() for s in transcript.split('.') if s.strip()]
                                                # Eliminar duplicados consecutivos
                                                unique_sentences = []
                                                last_sentence = ""
                                                for sentence in sentences:
                                                    if sentence.lower() != last_sentence.lower():
                                                        unique_sentences.append(sentence)
                                                        last_sentence = sentence
                                                # Reconstruir
                                                cleaned_transcript = '. '.join(unique_sentences)
                                                if cleaned_transcript and not cleaned_transcript.endswith('.'):
                                                    cleaned_transcript += '.'
                                                
                                                # Actualizar transcripción solo si es más larga
                                                if cleaned_transcript and len(cleaned_transcript) > len(last_transcript):
                                                    last_transcript = cleaned_transcript
                                                    last_update_time = asyncio.get_event_loop().time()
                                                    print(f"📝 [{lang.upper()}] ... {cleaned_transcript}")
                            except json.JSONDecodeError:
                                continue
                            except Exception as e:
                                print(f"⚠️ [{lang.upper()}] Error procesando mensaje: {e}")
                    except Exception as e:
                        silence_task.cancel()
                        print(f"⚠️ [{lang.upper()}] Conexión cerrada (texto): {e}")
                        # Enviar última transcripción si existe y no se envió antes
                        if last_transcript and last_transcript != last_sent_transcript:
                            print(f"📝 [{lang.upper()}] ⚠️ Usando última transcripción: {last_transcript}")
                            try:
                                await text_queue.put(last_transcript)
                            except:
                                pass
                
                await asyncio.gather(send_audio(), receive_text())
                
        except Exception as e:
            reconnect_count += 1
            print(f"⚠️ [{lang.upper()}] Deepgram desconectado. Reconectando en 2s...")
            await asyncio.sleep(2)

### ========== TEXT-TO-SPEECH (ElevenLabs REST API - SIMPLE Y CONFIABLE) ==========
def synthesize_speech_rest(text: str) -> bytes:
    """Sintetiza voz usando ElevenLabs REST API - Devuelve PCM directamente"""
    # Usar output_format=pcm_16000 para recibir PCM directamente
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}?output_format=pcm_16000"
    headers = {
        "Accept": "audio/pcm",
        "Content-Type": "application/json",
        "xi-api-key": ELEVEN_KEY
    }
    data = {
        "text": text,
        "model_id": "eleven_turbo_v2_5",
        "voice_settings": {
            "stability": config.VOICE_STABILITY,
            "similarity_boost": config.VOICE_SIMILARITY
        }
    }
    
    try:
        print(f"📡 Llamando ElevenLabs REST API...")
        response = requests.post(url, json=data, headers=headers, timeout=30)
        response.raise_for_status()
        audio_bytes = response.content
        print(f"✅ Recibidos {len(audio_bytes)} bytes de audio PCM")
        return audio_bytes
    except Exception as e:
        print(f"⚠️ Error en REST API: {e}")
        return b""

async def elevenlabs_tts(text_queue: asyncio.Queue, output_device: int, lang_label: str):
    """Sintetiza voz usando REST API y la reproduce"""
    stream = sd.RawOutputStream(
        samplerate=16000,
        channels=1,
        dtype='int16',
        device=output_device,
        blocksize=BLOCK_SAMPLES
    )
    stream.start()
    print(f"🔊 [{lang_label}] TTS REST iniciado")
    
    try:
        while True:
            text = await text_queue.get()
            if text is None:
                break
            
            print(f"🗣️ [{lang_label}] Sintetizando: {text}")
            
            # Ejecutar síntesis en thread pool (no bloquear asyncio)
            loop = asyncio.get_event_loop()
            audio_pcm = await loop.run_in_executor(None, synthesize_speech_rest, text)
            
            if not audio_pcm:
                print(f"⚠️ [{lang_label}] No se recibió audio")
                continue
            
            try:
                print(f"🔊 [{lang_label}] Reproduciendo {len(audio_pcm)} bytes")
                
                # Silencio previo (500ms)
                pre_silence = np.zeros(int(16000 * 0.5), dtype=np.int16)
                stream.write(pre_silence.tobytes())
                
                # AUDIO COMPLETO de una vez
                stream.write(audio_pcm)
                
                # Silencio posterior (2 segundos para asegurar)
                post_silence = np.zeros(int(16000 * 2.0), dtype=np.int16)
                stream.write(post_silence.tobytes())
                
                print(f"✅ [{lang_label}] Reproducción completada (500ms + {len(audio_pcm)}B + 2000ms)")
                
            except Exception as e:
                print(f"⚠️ [{lang_label}] Error reproduciendo: {e}")
                import traceback
                traceback.print_exc()
                
    finally:
        stream.stop()
        stream.close()

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
        
        # Procesar en bloques de 30ms
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
                        if self.sent_chunks % 100 == 0:  # Reducido: log cada 100 bloques (~3s)
                            print(f"🎵 [{self.name}] Enviados {self.sent_chunks} bloques de audio a la cola")
                else:
                    if self.is_speaking:
                        self.silence_frames += 1
                        # Enviar algunos frames de silencio después de hablar
                        if self.silence_frames < 30:  # ~900ms de silencio (aumentado)
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
        """Inicia la captura de audio"""
        self.stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype='int16',
            device=self.device_idx,
            blocksize=BLOCK_SAMPLES,
            callback=self.callback
        )
        self.stream.start()
        print(f"🎤 [{self.name}] Captura iniciada")
    
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
    print("🌍 TRADUCTOR EN TIEMPO REAL CON IA")
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
    mic_audio_q = asyncio.Queue(maxsize=500)  # Aumentado para evitar QueueFull
    es_text_q = asyncio.Queue(maxsize=50)
    en_tts_text_q = asyncio.Queue(maxsize=50)
    
    # VOZ DE OTROS: Inglés → Español
    meeting_audio_q = asyncio.Queue(maxsize=500)  # Aumentado para evitar QueueFull
    en_text_q = asyncio.Queue(maxsize=50)
    es_tts_text_q = asyncio.Queue(maxsize=50)
    
    # Timestamps para cancelación de eco
    last_en_synthesis = {'time': 0}  # Última vez que se sintetizó en inglés
    last_es_synthesis = {'time': 0}  # Última vez que se sintetizó en español
    
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
            text_en = translate_text(text_es, "EN-US")
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
            
            # Verificar si este audio es eco del sistema (menos de 8 segundos desde última síntesis EN)
            current_time = asyncio.get_event_loop().time()
            time_since_synthesis = current_time - last_en_synthesis['time']
            
            if time_since_synthesis < 8.0:
                print(f"� Ignorando eco del sistema (EN): '{text_en}' ({time_since_synthesis:.1f}s desde síntesis)")
                continue
            
            print(f"�🔄 Traduciendo EN→ES: '{text_en}'")
            text_es = translate_text(text_en, "ES")
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
        # STT
        asyncio.create_task(deepgram_stt(mic_audio_q, "es", es_text_q)),
        asyncio.create_task(deepgram_stt(meeting_audio_q, "en", en_text_q)),
        
        # Traducción
        asyncio.create_task(translate_es_to_en()),
        asyncio.create_task(translate_en_to_es()),
        
        # TTS
        asyncio.create_task(elevenlabs_tts(en_tts_text_q, vb_input_idx, "EN→REUNIÓN")),
        asyncio.create_task(elevenlabs_tts(es_tts_text_q, speakers_idx, "ES→TÚ")),
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
