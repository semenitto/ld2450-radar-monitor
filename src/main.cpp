#include <Arduino.h>
#include <DNSServer.h>
#include <ESPmDNS.h>
#include <WebServer.h>
#include <WiFi.h>
#include <math.h>

namespace {

constexpr char AP_SSID[] = "LD2450-Radar";
constexpr char AP_PASSWORD[] = "radar2450";
constexpr char MDNS_NAME[] = "ld2450";

constexpr uint32_t RADAR_BAUD = 256000;
constexpr int RADAR_RX_PIN = 16;  // ESP32 RX2 <- LD2450 TX
constexpr int RADAR_TX_PIN = 17;  // ESP32 TX2 -> LD2450 RX
constexpr size_t RADAR_FRAME_SIZE = 30;

struct Target {
  int16_t xMm = 0;
  int16_t yMm = 0;
  int16_t speedCms = 0;
  uint16_t resolutionMm = 0;
  bool valid = false;
};

HardwareSerial radarSerial(2);
WebServer server(80);
DNSServer dnsServer;

Target targets[3];
uint8_t radarFrame[RADAR_FRAME_SIZE];
size_t radarFramePos = 0;
uint32_t lastFrameMs = 0;
uint32_t frameCount = 0;

const char INDEX_HTML[] PROGMEM = R"HTML(
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <title>LD2450 · радар пространства</title>
  <style>
    :root{color-scheme:dark;--bg:#071014;--panel:#0d1a20;--line:#1d343e;--text:#e9f7fb;--muted:#89a4af;--cyan:#35e5ff;--pink:#ff5c93;--amber:#ffc857}
    *{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 50% -20%,#16313c 0,#071014 48%);color:var(--text);font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;min-height:100vh}
    main{max-width:1180px;margin:auto;padding:24px}.top{display:flex;justify-content:space-between;gap:16px;align-items:center;margin-bottom:18px}h1{font-size:clamp(22px,4vw,34px);margin:0;letter-spacing:-.03em}.sub{color:var(--muted);margin-top:4px}
    .badge{display:flex;align-items:center;gap:8px;background:#102129;border:1px solid #24414d;border-radius:999px;padding:8px 13px;white-space:nowrap}.dot{width:9px;height:9px;border-radius:50%;background:#72838a;box-shadow:0 0 0 4px #72838a22}.online .dot{background:#55ef9f;box-shadow:0 0 14px #55ef9f}.offline .dot{background:#ff6b6b;box-shadow:0 0 14px #ff6b6b}
    .layout{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:16px}.panel{background:linear-gradient(180deg,#0d1b21eF,#0a151aef);border:1px solid #1b343e;border-radius:18px;box-shadow:0 22px 70px #0005;overflow:hidden}.canvas-wrap{position:relative;min-height:620px}canvas{display:block;width:100%;height:620px}.legend{position:absolute;top:14px;left:14px;background:#071014c9;border:1px solid #24414d;border-radius:10px;padding:8px 10px;color:var(--muted);font-size:12px;backdrop-filter:blur(8px)}
    aside{display:flex;flex-direction:column;gap:12px}.target{padding:16px;border:1px solid #1d343e;border-radius:14px;background:#0d1a20;position:relative;overflow:hidden}.target:before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--c)}.target h2{font-size:14px;margin:0 0 12px;color:var(--c)}.metric{display:grid;grid-template-columns:1fr 1fr;gap:8px}.metric div{background:#081217;border-radius:9px;padding:8px}.metric span{display:block;color:var(--muted);font-size:11px}.metric b{font-size:15px}.empty{opacity:.48}
    .stats{padding:14px 16px;display:grid;grid-template-columns:1fr 1fr;gap:9px}.stats div{background:#081217;border-radius:9px;padding:9px}.stats span{display:block;color:var(--muted);font-size:11px}.note{color:var(--muted);font-size:12px;padding:13px 16px;border-top:1px solid #1b343e}.warn{color:#ffd27a}
    @media(max-width:800px){main{padding:14px}.top{align-items:flex-start;flex-direction:column}.layout{grid-template-columns:1fr}.canvas-wrap{min-height:480px}canvas{height:480px}aside{display:grid;grid-template-columns:1fr}.target{min-height:142px}}
  </style>
</head>
<body>
<main>
  <div class="top">
    <div><h1>LD2450 · карта целей</h1><div class="sub">ESP32 · UART2 256000 бод · обновление 10 Гц</div></div>
    <div id="badge" class="badge"><i class="dot"></i><span id="connection">Ожидание радара</span></div>
  </div>
  <div class="layout">
    <section class="panel canvas-wrap">
      <canvas id="radar"></canvas>
      <div class="legend">Радар внизу по центру<br>X: поперёк · Y: вперёд · сетка 1 м</div>
    </section>
    <aside>
      <div id="target0" class="target empty" style="--c:var(--cyan)"><h2>Цель 1</h2><div class="metric"><div><span>X</span><b data-x>—</b></div><div><span>Y</span><b data-y>—</b></div><div><span>Скорость</span><b data-speed>—</b></div><div><span>Дистанция</span><b data-distance>—</b></div></div></div>
      <div id="target1" class="target empty" style="--c:var(--pink)"><h2>Цель 2</h2><div class="metric"><div><span>X</span><b data-x>—</b></div><div><span>Y</span><b data-y>—</b></div><div><span>Скорость</span><b data-speed>—</b></div><div><span>Дистанция</span><b data-distance>—</b></div></div></div>
      <div id="target2" class="target empty" style="--c:var(--amber)"><h2>Цель 3</h2><div class="metric"><div><span>X</span><b data-x>—</b></div><div><span>Y</span><b data-y>—</b></div><div><span>Скорость</span><b data-speed>—</b></div><div><span>Дистанция</span><b data-distance>—</b></div></div></div>
      <section class="panel">
        <div class="stats"><div><span>Активные цели</span><b id="count">0</b></div><div><span>Кадры UART</span><b id="frames">0</b></div><div><span>Возраст кадра</span><b id="age">—</b></div><div><span>Адрес</span><b>192.168.4.1</b></div></div>
        <div class="note"><span class="warn">Важно:</span> LD2450 видит движущиеся цели; неподвижный человек может исчезать из трекинга. Отображение ограничено ±6 м по X и 6 м по Y.</div>
      </section>
    </aside>
  </div>
</main>
<script>
const canvas=document.getElementById('radar'),ctx=canvas.getContext('2d');
const colors=['#35e5ff','#ff5c93','#ffc857'];
let state={connected:false,targets:[],frames:0,frameAgeMs:null},lastFrames=-1,busy=false;
const trails=[[],[],[]];

function resize(){const r=canvas.getBoundingClientRect(),d=Math.min(devicePixelRatio||1,2);canvas.width=Math.round(r.width*d);canvas.height=Math.round(r.height*d);ctx.setTransform(d,0,0,d,0,0)}
new ResizeObserver(resize).observe(canvas);resize();

function updateCards(){
  let count=0;
  for(let i=0;i<3;i++){
    const t=state.targets[i]||{valid:false},card=document.getElementById('target'+i);
    card.classList.toggle('empty',!t.valid);
    const put=(q,v)=>card.querySelector(q).textContent=v;
    if(t.valid){count++;put('[data-x]',(t.x/1000).toFixed(2)+' м');put('[data-y]',(t.y/1000).toFixed(2)+' м');put('[data-speed]',t.speed+' см/с');put('[data-distance]',(t.distance/1000).toFixed(2)+' м')}
    else{put('[data-x]','—');put('[data-y]','—');put('[data-speed]','—');put('[data-distance]','—')}
  }
  document.getElementById('count').textContent=count;document.getElementById('frames').textContent=state.frames;
  document.getElementById('age').textContent=state.frameAgeMs===null?'—':state.frameAgeMs+' мс';
  const badge=document.getElementById('badge');badge.classList.toggle('online',state.connected);badge.classList.toggle('offline',!state.connected);
  document.getElementById('connection').textContent=state.connected?'Радар передаёт данные':'Нет данных UART';
}

async function poll(){if(busy)return;busy=true;try{const r=await fetch('/api/targets',{cache:'no-store'});if(!r.ok)throw Error();state=await r.json();if(state.frames!==lastFrames){state.targets.forEach((t,i)=>{if(t.valid){trails[i].push({x:t.x,y:t.y});if(trails[i].length>28)trails[i].shift()}});lastFrames=state.frames}updateCards()}catch(e){state.connected=false;updateCards()}finally{busy=false}}
setInterval(poll,100);poll();

function draw(){
  const w=canvas.clientWidth,h=canvas.clientHeight,ox=w/2,oy=h-25,range=6000,scale=Math.min((w-44)/(range*2),(h-58)/range);
  ctx.clearRect(0,0,w,h);
  ctx.save();ctx.beginPath();ctx.moveTo(ox,oy);for(let a=-60;a<=60;a+=2){let r=a*Math.PI/180;ctx.lineTo(ox+Math.sin(r)*range*scale,oy-Math.cos(r)*range*scale)}ctx.closePath();ctx.fillStyle='#0d293344';ctx.fill();ctx.restore();
  ctx.strokeStyle='#1d3b46';ctx.lineWidth=1;ctx.fillStyle='#6f929e';ctx.font='11px system-ui';
  for(let m=1;m<=6;m++){const r=m*1000*scale;ctx.beginPath();ctx.arc(ox,oy,r,Math.PI*7/6,Math.PI*11/6);ctx.stroke();ctx.fillText(m+' м',ox+5,oy-r+13)}
  [-60,-30,0,30,60].forEach(a=>{const r=a*Math.PI/180;ctx.beginPath();ctx.moveTo(ox,oy);ctx.lineTo(ox+Math.sin(r)*range*scale,oy-Math.cos(r)*range*scale);ctx.stroke()});
  ctx.strokeStyle='#31515d';ctx.beginPath();ctx.moveTo(22,oy);ctx.lineTo(w-22,oy);ctx.stroke();
  for(let i=0;i<3;i++){
    const tr=trails[i];if(tr.length>1){ctx.beginPath();tr.forEach((p,j)=>{const x=ox+p.x*scale,y=oy-p.y*scale;j?ctx.lineTo(x,y):ctx.moveTo(x,y)});ctx.strokeStyle=colors[i]+'66';ctx.lineWidth=2;ctx.stroke()}
    const t=state.targets[i];if(!t||!t.valid)continue;const x=ox+t.x*scale,y=oy-t.y*scale;if(x<0||x>w||y<0||y>h)continue;
    const pulse=13+Math.sin(performance.now()/220+i)*2;ctx.beginPath();ctx.arc(x,y,pulse,0,Math.PI*2);ctx.fillStyle=colors[i]+'22';ctx.fill();ctx.beginPath();ctx.arc(x,y,6,0,Math.PI*2);ctx.fillStyle=colors[i];ctx.shadowColor=colors[i];ctx.shadowBlur=16;ctx.fill();ctx.shadowBlur=0;ctx.fillStyle='#e9f7fb';ctx.font='bold 12px system-ui';ctx.fillText(String(i+1),x+11,y-9)
  }
  ctx.fillStyle='#35e5ff';ctx.beginPath();ctx.moveTo(ox,oy-10);ctx.lineTo(ox-8,oy+5);ctx.lineTo(ox+8,oy+5);ctx.closePath();ctx.fill();requestAnimationFrame(draw)
}
requestAnimationFrame(draw);
</script>
</body>
</html>
)HTML";

int16_t decodeSignedMagnitude(uint16_t raw) {
  if (raw & 0x8000U) {
    return static_cast<int16_t>(raw - 0x8000U);
  }
  return -static_cast<int16_t>(raw);
}

uint16_t readLe16(const uint8_t *data) {
  return static_cast<uint16_t>(data[0]) |
         (static_cast<uint16_t>(data[1]) << 8U);
}

void streamTargetsOverUsb() {
  // Stable, machine-readable ASCII protocol for the Windows companion app:
  // LD2450_DATA,uptime_ms,frame,valid,x_mm,y_mm,speed_cm_s,resolution_mm,...
  char line[256];
  int used = snprintf(line, sizeof(line), "LD2450_DATA,%lu,%lu",
                      static_cast<unsigned long>(millis()),
                      static_cast<unsigned long>(frameCount));
  for (size_t i = 0; i < 3 && used > 0 && used < static_cast<int>(sizeof(line)); ++i) {
    const Target &t = targets[i];
    used += snprintf(line + used, sizeof(line) - used, ",%u,%d,%d,%d,%u",
                     t.valid ? 1U : 0U,
                     static_cast<int>(t.xMm),
                     static_cast<int>(t.yMm),
                     static_cast<int>(t.speedCms),
                     static_cast<unsigned int>(t.resolutionMm));
  }
  if (used > 0 && used < static_cast<int>(sizeof(line))) {
    Serial.println(line);
  }
}

void parseRadarFrame(const uint8_t *frame) {
  for (size_t i = 0; i < 3; ++i) {
    const size_t p = 4 + i * 8;
    const uint16_t rawX = readLe16(frame + p);
    const uint16_t rawY = readLe16(frame + p + 2);
    const uint16_t rawSpeed = readLe16(frame + p + 4);
    const uint16_t resolution = readLe16(frame + p + 6);

    targets[i].xMm = decodeSignedMagnitude(rawX);
    targets[i].yMm = decodeSignedMagnitude(rawY);
    targets[i].speedCms = decodeSignedMagnitude(rawSpeed);
    targets[i].resolutionMm = resolution;
    targets[i].valid = rawX != 0 || rawY != 0 || rawSpeed != 0 || resolution != 0;
  }
  lastFrameMs = millis();
  ++frameCount;
  streamTargetsOverUsb();
}

void ingestRadarByte(uint8_t value) {
  static constexpr uint8_t header[4] = {0xAA, 0xFF, 0x03, 0x00};

  if (radarFramePos < 4) {
    if (value == header[radarFramePos]) {
      radarFrame[radarFramePos++] = value;
    } else {
      radarFramePos = value == header[0] ? 1 : 0;
      if (radarFramePos == 1) radarFrame[0] = value;
    }
    return;
  }

  radarFrame[radarFramePos++] = value;
  if (radarFramePos == RADAR_FRAME_SIZE) {
    if (radarFrame[28] == 0x55 && radarFrame[29] == 0xCC) {
      parseRadarFrame(radarFrame);
    }
    radarFramePos = 0;
  }
}

void readRadar() {
  while (radarSerial.available() > 0) {
    ingestRadarByte(static_cast<uint8_t>(radarSerial.read()));
  }
}

void sendIndex() {
  server.sendHeader("Cache-Control", "no-store");
  server.send_P(200, "text/html; charset=utf-8", INDEX_HTML);
}

void sendTargets() {
  const bool hasFrame = frameCount > 0;
  const uint32_t age = hasFrame ? millis() - lastFrameMs : 0;
  const bool connected = hasFrame && age < 1500;

  String json;
  json.reserve(640);
  json += F("{\"connected\":");
  json += connected ? F("true") : F("false");
  json += F(",\"frameAgeMs\":");
  json += hasFrame ? String(age) : String(F("null"));
  json += F(",\"frames\":");
  json += frameCount;
  json += F(",\"uptimeMs\":");
  json += millis();
  json += F(",\"targets\":[");

  for (size_t i = 0; i < 3; ++i) {
    if (i) json += ',';
    const Target &t = targets[i];
    const float distance = sqrtf(static_cast<float>(t.xMm) * t.xMm +
                                 static_cast<float>(t.yMm) * t.yMm);
    const float angle = atan2f(static_cast<float>(t.xMm),
                               static_cast<float>(t.yMm)) * 180.0f / PI;
    json += F("{\"valid\":");
    json += t.valid ? F("true") : F("false");
    json += F(",\"x\":"); json += t.xMm;
    json += F(",\"y\":"); json += t.yMm;
    json += F(",\"speed\":"); json += t.speedCms;
    json += F(",\"resolution\":"); json += t.resolutionMm;
    json += F(",\"distance\":"); json += String(distance, 1);
    json += F(",\"angle\":"); json += String(angle, 1);
    json += '}';
  }
  json += F("]}");

  server.sendHeader("Cache-Control", "no-store, no-cache, must-revalidate");
  server.send(200, "application/json; charset=utf-8", json);
}

void configureWebServer() {
  server.on("/", HTTP_GET, sendIndex);
  server.on("/api/targets", HTTP_GET, sendTargets);
  server.on("/health", HTTP_GET, [] { server.send(200, "text/plain", "ok"); });

  // Common captive-portal probes used by Windows, Android and Apple devices.
  server.on("/generate_204", HTTP_GET, sendIndex);
  server.on("/hotspot-detect.html", HTTP_GET, sendIndex);
  server.on("/connecttest.txt", HTTP_GET, sendIndex);
  server.on("/ncsi.txt", HTTP_GET, sendIndex);
  server.onNotFound(sendIndex);
  server.begin();
}

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(250);
  Serial.println();
  Serial.println(F("LD2450 Radar UI starting"));

  radarSerial.setRxBufferSize(2048);
  radarSerial.begin(RADAR_BAUD, SERIAL_8N1, RADAR_RX_PIN, RADAR_TX_PIN);

  WiFi.mode(WIFI_AP);
  WiFi.setSleep(false);
  const bool apStarted = WiFi.softAP(AP_SSID, AP_PASSWORD, 6, false, 4);
  const IPAddress apIp = WiFi.softAPIP();

  dnsServer.start(53, "*", apIp);
  MDNS.begin(MDNS_NAME);
  MDNS.addService("http", "tcp", 80);
  configureWebServer();

  Serial.printf("AP: %s (%s)\n", AP_SSID, apStarted ? "started" : "failed");
  Serial.printf("Open: http://%s or http://%s.local\n", apIp.toString().c_str(), MDNS_NAME);
  Serial.printf("Radar UART2: RX GPIO%d, TX GPIO%d, %lu baud\n",
                RADAR_RX_PIN, RADAR_TX_PIN,
                static_cast<unsigned long>(RADAR_BAUD));
}

void loop() {
  readRadar();
  dnsServer.processNextRequest();
  server.handleClient();
  delay(1);
}
