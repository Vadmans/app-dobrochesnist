import { useEffect, useMemo, useState, useRef } from "react";
import { Capacitor } from "@capacitor/core";
import { PushNotifications } from "@capacitor/push-notifications";
import { LocalNotifications } from "@capacitor/local-notifications";
import { Filesystem, Directory, Encoding } from "@capacitor/filesystem";
import { FileOpener } from "@capawesome-team/capacitor-file-opener";
import {
  AlertTriangle,
  ArrowRight,
  Bell,
  BellRing,
  BookOpen,
  CalendarDays,
  CalendarPlus,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Clock,
  Compass,
  ExternalLink,
  FileText,
  Home,
  Loader2,
  MessageCircle,
  RefreshCw,
  RotateCcw,
  Search,
  Send,
  ShieldCheck,
  Wifi,
  WifiOff,
  X,
  User,
  Bot,
  Clock as ClockIcon,
} from "lucide-react";

const API_BASE = "https://app-dobrochesnist.onrender.com";
const POLL_MS = 3 * 60 * 1000;
const NOTIFY_HOUR = 9;
const SEEN_KEY = "seenEventIds";
const REMINDERS_KEY = "dobroReminders";
const CLIENT_ID_KEY = "clientId";

const C = {
  bg: "#EEF3F7",
  surface: "#FFFFFF",
  ink: "#17212F",
  inkSoft: "#5A6577",
  inkFaint: "#8A94A3",
  line: "#DCE1E6",
  accent: "#174C68",
  accent2: "#2F7EA4",
  accentSoft: "#E6EEF2",
  amber: "#A9690A",
  amberSoft: "#F8EFD9",
  red: "#9E2F3C",
  redSoft: "#F5E2E4",
  green: "#2C6A4E",
  greenSoft: "#E1EEE8",
  purple: "#5B4E8E",
  purpleSoft: "#EBE7F4",
};

const serif = "Georgia, 'Times New Roman', serif";
const sans = "system-ui, -apple-system, 'Segoe UI', sans-serif";

const CATS = {
  declaration: { label: "Декларування", color: C.accent, soft: C.accentSoft },
  conflict: { label: "Конфлікт інтересів", color: C.red, soft: C.redSoft },
  gifts: { label: "Подарунки", color: C.amber, soft: C.amberSoft },
  notice: { label: "Повідомлення", color: C.purple, soft: C.purpleSoft },
  training: { label: "Навчання", color: C.green, soft: C.greenSoft },
  restriction: { label: "Обмеження", color: "#6B5B49", soft: "#EFE9E2" },
};

const NAV = [
  { id: "home", label: "Головна", Icon: Home },
  { id: "calendar", label: "Календар", Icon: CalendarDays },
  { id: "chat", label: "Чат", Icon: MessageCircle },
  { id: "reference", label: "Довідник", Icon: BookOpen },
];

const today = new Date();
today.setHours(0, 0, 0, 0);

const MONTHS = ["січня", "лютого", "березня", "квітня", "травня", "червня", "липня", "серпня", "вересня", "жовтня", "листопада", "грудня"];
const MONTHS_N = ["Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень", "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень"];
const WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"];

const getClientId = () => {
  let id = localStorage.getItem(CLIENT_ID_KEY);
  if (!id) {
    id = `client_${Date.now()}_${Math.random().toString(36).substring(2, 10)}`;
    localStorage.setItem(CLIENT_ID_KEY, id);
  }
  return id;
};

const parseDate = (s) => {
  if (!s) return new Date();
  const [y, m, d] = String(s).split("-").map(Number);
  return new Date(y, m - 1, d);
};

const fmtDate = (d) => `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
const fmtDateTime = (d) => {
  if (!d) return "";
  const date = new Date(d);
  return `${date.getDate().toString().padStart(2, "0")}.${(date.getMonth() + 1).toString().padStart(2, "0")}.${date.getFullYear()} ${date.getHours().toString().padStart(2, "0")}:${date.getMinutes().toString().padStart(2, "0")}`;
};
const daysUntil = (d) => Math.round((d - today) / 86400000);

function urgency(days) {
  if (days < 0) return { label: "Минуло", color: C.inkFaint, soft: C.line, Icon: CheckCircle2 };
  if (days <= 3) return { label: "Терміново", color: C.red, soft: C.redSoft, Icon: AlertTriangle };
  if (days <= 10) return { label: "Скоро", color: C.amber, soft: C.amberSoft, Icon: Clock };
  return { label: "Планово", color: C.green, soft: C.greenSoft, Icon: CalendarDays };
}

function loadJson(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key) || "null") ?? fallback; } catch { return fallback; }
}
function saveJson(key, value) { localStorage.setItem(key, JSON.stringify(value)); }

function openLink(url) {
  if (!url) return;
  window.open(url, "_blank");
}

function safeIcsText(v = "") {
  return String(v).replace(/\\/g, "\\\\").replace(/;/g, "\\;").replace(/,/g, "\\,").replace(/\r?\n/g, "\\n");
}
function buildICS(events) {
  const pad = (n) => String(n).padStart(2, "0");
  const f = (d) => `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}`;
  const lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Dobrochesnist//Calendar//UK"];
  events.forEach((ev) => {
    lines.push(
      "BEGIN:VEVENT",
      `UID:${ev.id}@dobrochesnist`,
      `DTSTART;VALUE=DATE:${f(ev.date)}`,
      `SUMMARY:${safeIcsText(ev.title)}`,
      `DESCRIPTION:${safeIcsText(`${ev.description || ""}\n${ev.instruction || ""}`)}`,
      "END:VEVENT",
    );
  });
  lines.push("END:VCALENDAR");
  return lines.join("\r\n");
}
async function exportCalendar(events) {
  const future = events.filter((e) => e.date >= today);
  if (!future.length) return alert("Немає майбутніх подій для додавання.");
  const ics = buildICS(future);
  if (Capacitor.isNativePlatform()) {
    try {
      const { uri } = await Filesystem.writeFile({ path: "dobrochesnist.ics", data: ics, directory: Directory.Cache, encoding: Encoding.UTF8 });
      await FileOpener.openFile({ path: uri, mimeType: "text/calendar" });
      return;
    } catch {}
  }
  const url = URL.createObjectURL(new Blob([ics], { type: "text/calendar" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = "dobrochesnist.ics";
  a.click();
  URL.revokeObjectURL(url);
}

function notifId(eventId, offset) {
  const s = `${eventId}:${offset}`;
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return Math.abs(h) % 2147483647;
}

async function scheduleReminders(events, enabledMap) {
  if (!Capacitor.isNativePlatform()) return;
  try {
    const perm = await LocalNotifications.requestPermissions();
    if (perm.display !== "granted") return;
    const pending = await LocalNotifications.getPending();
    if (pending.notifications.length) {
      await LocalNotifications.cancel({ notifications: pending.notifications.map((n) => ({ id: n.id })) });
    }
    const now = new Date();
    const items = [];
    for (const ev of events) {
      if (enabledMap && enabledMap[ev.id] === false) continue;
      for (const off of ev.reminders || []) {
        const at = new Date(ev.date);
        at.setDate(at.getDate() - off);
        at.setHours(NOTIFY_HOUR, 0, 0, 0);
        if (at > now) {
          items.push({ id: notifId(ev.id, off), title: "Доброчесність", body: off === 0 ? `Сьогодні строк: ${ev.title}` : `За ${off} дн. — ${ev.title}`, schedule: { at } });
        }
      }
    }
    if (items.length) await LocalNotifications.schedule({ notifications: items });
  } catch {}
}

async function detectNewEvents(events) {
  if (!Capacitor.isNativePlatform()) return;
  let seen = [];
  try { seen = JSON.parse(localStorage.getItem(SEEN_KEY) || "[]"); } catch {}
  const ids = events.map((e) => e.id);
  if (!seen.length) {
    localStorage.setItem(SEEN_KEY, JSON.stringify(ids));
    return;
  }
  const seenSet = new Set(seen);
  const fresh = events.filter((e) => !seenSet.has(e.id));
  if (fresh.length) {
    try {
      await LocalNotifications.requestPermissions();
      await LocalNotifications.schedule({
        notifications: fresh.map((ev) => ({ id: notifId(ev.id, 999), title: "Нова подія", body: `${ev.title} — ${fmtDate(ev.date)}`, schedule: { at: new Date(Date.now() + 1200) } })),
      });
    } catch {}
  }
  localStorage.setItem(SEEN_KEY, JSON.stringify(ids));
}

function Badge({ cat }) {
  const c = CATS[cat] || { label: cat || "Інше", color: C.inkSoft, soft: C.bg };
  return <span className="inline-flex items-center rounded-full px-2.5 py-1 text-xs font-semibold" style={{ background: c.soft, color: c.color }}>{c.label}</span>;
}

function Btn({ children, onClick, variant = "ghost", Icon, disabled = false }) {
  const styles = variant === "primary"
    ? { background: C.accent, color: "#fff", boxShadow: "0 10px 24px rgba(23,76,104,.20)" }
    : variant === "soft"
      ? { background: C.accentSoft, color: C.accent }
      : variant === "danger"
        ? { background: C.redSoft, color: C.red }
        : { background: C.surface, color: C.ink, border: `1px solid ${C.line}` };
  return <button disabled={disabled} onClick={onClick} className="inline-flex items-center justify-center gap-2 rounded-xl px-3.5 py-2.5 text-sm font-semibold transition active:scale-[.98] disabled:opacity-50" style={styles}>{Icon && <Icon size={16} />}{children}</button>;
}

function EventModal({ event, onClose, onReminder, reminderOn, onCalendar }) {
  if (!event) return null;
  const d = daysUntil(event.date);
  const u = urgency(d);
  return <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/35 p-3 sm:items-center" onClick={onClose}>
    <div className="w-full max-w-lg rounded-3xl p-5" style={{ background: C.surface, color: C.ink }} onClick={(e) => e.stopPropagation()}>
      <div className="mb-3 flex items-start justify-between gap-3">
        <div>
          <Badge cat={event.cat} />
          <h2 className="mt-3 text-2xl leading-tight" style={{ fontFamily: serif }}>{event.title}</h2>
          <div className="mt-2 flex items-center gap-2 text-sm" style={{ color: u.color }}><u.Icon size={16} /> {fmtDate(event.date)} · {d < 0 ? "строк минув" : `залишилось ${d} дн.`}</div>
        </div>
        <button className="rounded-xl p-2" style={{ background: C.bg }} onClick={onClose}><X size={18} /></button>
      </div>
      {event.description && <p className="mt-3 text-sm leading-6" style={{ color: C.inkSoft }}>{event.description}</p>}
      {event.instruction && <div className="mt-4 rounded-2xl p-4" style={{ background: C.accentSoft }}><div className="mb-1 text-xs font-bold uppercase" style={{ color: C.accent }}>Інструкція</div><div className="text-sm leading-6" style={{ color: C.ink }}>{event.instruction}</div></div>}
      <div className="mt-5 flex flex-wrap gap-2">
        {event.link && <Btn variant="primary" Icon={ExternalLink} onClick={() => openLink(event.link)}>Відкрити</Btn>}
        <Btn Icon={CalendarPlus} onClick={() => onCalendar([event])}>В календар</Btn>
        <Btn variant={reminderOn ? "soft" : "ghost"} Icon={reminderOn ? BellRing : Bell} onClick={() => onReminder(event.id)}>{reminderOn ? "Нагадування увімкнено" : "Увімкнути нагадування"}</Btn>
      </div>
    </div>
  </div>;
}

function Dashboard({ events, reminders, toggleReminder, onAddCalendar, onOpenEvent }) {
  const sorted = [...events].sort((a, b) => a.date - b.date);
  const future = sorted.filter((e) => e.date >= today);
  const next = future[0] || sorted[0];
  if (!next) {
    return <div className="rounded-3xl p-6 text-sm" style={{ background: C.surface, border: `1px solid ${C.line}`, color: C.inkSoft }}>Поки що немає запланованих подій.</div>;
  }
  const d = daysUntil(next.date);
  const u = urgency(d);
  return <div className="space-y-5">
    <div className="overflow-hidden rounded-3xl" style={{ background: C.surface, border: `1px solid ${C.line}`, boxShadow: "0 18px 50px rgba(23,76,104,.10)" }}>
      <div className="p-5 sm:p-6" style={{ background: `linear-gradient(135deg, ${u.soft}, #fff)` }}>
        <div className="flex items-start gap-4">
          <div className="rounded-2xl p-3" style={{ background: C.surface, color: u.color }}><u.Icon size={24} /></div>
          <div className="min-w-0 flex-1">
            <div className="text-xs font-bold uppercase tracking-wide" style={{ color: u.color }}>{u.label} · найближча дія</div>
            <h2 className="mt-2 text-2xl leading-tight sm:text-3xl" style={{ fontFamily: serif, color: C.ink }}>{next.title}</h2>
            <p className="mt-2 text-sm" style={{ color: C.inkSoft }}>Строк — {fmtDate(next.date)}. {d >= 0 ? <b style={{ color: u.color }}>Залишилось {d} дн.</b> : <b>Строк минув.</b>}</p>
            {next.instruction && <p className="mt-3 text-sm leading-6" style={{ color: C.ink }}>{next.instruction}</p>}
            <div className="mt-4 flex flex-wrap gap-2">
              <Btn variant="primary" Icon={FileText} onClick={() => onOpenEvent(next)}>Деталі</Btn>
              <Btn Icon={CalendarPlus} onClick={() => onAddCalendar([next])}>В календар</Btn>
              <Btn variant={reminders[next.id] !== false ? "soft" : "ghost"} Icon={reminders[next.id] !== false ? BellRing : Bell} onClick={() => toggleReminder(next.id)}>{reminders[next.id] !== false ? "Нагадування увімкнено" : "Увімкнути"}</Btn>
            </div>
          </div>
        </div>
      </div>
    </div>

    <div>
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-bold uppercase tracking-wide" style={{ color: C.inkFaint }}>Майбутні події</h3>
        <button onClick={() => onAddCalendar(future)} className="inline-flex items-center gap-1 text-xs font-semibold" style={{ color: C.accent }}><CalendarPlus size={14} /> Усі в календар</button>
      </div>
      <div className="space-y-2 overflow-y-auto pr-1" style={{ maxHeight: "430px" }}>
        {future.map((ev) => <EventRow key={ev.id} ev={ev} reminders={reminders} onOpen={onOpenEvent} toggleReminder={toggleReminder} />)}
      </div>
    </div>
  </div>;
}

function EventRow({ ev, reminders, toggleReminder, onOpen }) {
  const dd = daysUntil(ev.date);
  const uu = urgency(dd);
  return <div className="flex items-center gap-3 rounded-2xl px-3 py-3 sm:px-4" style={{ background: C.surface, border: `1px solid ${C.line}` }}>
    <button onClick={() => onOpen(ev)} className="flex w-16 shrink-0 flex-col items-center justify-center rounded-xl py-2" style={{ background: uu.soft }}>
      <span className="text-lg font-bold leading-none" style={{ color: uu.color }}>{dd < 0 ? "✓" : dd}</span>
      <span className="text-[11px]" style={{ color: uu.color }}>{dd < 0 ? "минуло" : "днів"}</span>
    </button>
    <button onClick={() => onOpen(ev)} className="min-w-0 flex-1 text-left">
      <span className="block truncate text-sm font-semibold" style={{ color: C.ink }}>{ev.title}</span>
      <div className="mt-1 flex flex-wrap items-center gap-2"><Badge cat={ev.cat} /><span className="text-xs" style={{ color: C.inkFaint }}>{fmtDate(ev.date)}</span></div>
    </button>
    {ev.link && <button onClick={() => openLink(ev.link)} className="rounded-xl p-2" style={{ color: C.accent }}><ExternalLink size={18} /></button>}
    <button onClick={() => toggleReminder(ev.id)} className="rounded-xl p-2" style={{ color: reminders[ev.id] !== false ? C.accent : C.inkFaint }}>{reminders[ev.id] !== false ? <BellRing size={18} /> : <Bell size={18} />}</button>
  </div>;
}

function CalendarView({ events, onOpenEvent }) {
  const [cursor, setCursor] = useState(new Date(today.getFullYear(), today.getMonth(), 1));
  const y = cursor.getFullYear(), m = cursor.getMonth();
  const startWd = (new Date(y, m, 1).getDay() + 6) % 7;
  const daysInMonth = new Date(y, m + 1, 0).getDate();
  const cells = [];
  for (let i = 0; i < startWd; i++) cells.push(null);
  for (let day = 1; day <= daysInMonth; day++) cells.push(day);
  const evByDay = {};
  events.forEach((ev) => {
    if (ev.date.getFullYear() === y && ev.date.getMonth() === m) (evByDay[ev.date.getDate()] = evByDay[ev.date.getDate()] || []).push(ev);
  });
  return <div className="rounded-3xl p-4 sm:p-5" style={{ background: C.surface, border: `1px solid ${C.line}` }}>
    <div className="mb-4 flex items-center justify-between">
      <h2 className="text-xl" style={{ fontFamily: serif, color: C.ink }}>{MONTHS_N[m]} {y}</h2>
      <div className="flex gap-1"><Btn Icon={ChevronLeft} onClick={() => setCursor(new Date(y, m - 1, 1))} /><Btn Icon={ChevronRight} onClick={() => setCursor(new Date(y, m + 1, 1))} /></div>
    </div>
    <div className="grid grid-cols-7 gap-1">
      {WD.map((w) => <div key={w} className="pb-1 text-center text-xs font-bold" style={{ color: C.inkFaint }}>{w}</div>)}
      {cells.map((day, i) => {
        const isToday = day === today.getDate() && m === today.getMonth() && y === today.getFullYear();
        const evs = day ? evByDay[day] : null;
        return <div key={i} className="min-h-[76px] rounded-xl p-1" style={{ background: day ? (isToday ? C.accentSoft : C.bg) : "transparent", border: isToday ? `1px solid ${C.accent}` : "1px solid transparent" }}>
          {day && <><div className="text-xs font-bold" style={{ color: isToday ? C.accent : C.inkSoft }}>{day}</div><div className="mt-1 space-y-1">{evs?.slice(0, 2).map((ev) => <button key={ev.id} onClick={() => onOpenEvent(ev)} className="block w-full truncate rounded-lg px-1 py-0.5 text-left text-[11px] font-semibold" style={{ background: (CATS[ev.cat] || {}).soft || C.bg, color: (CATS[ev.cat] || {}).color || C.ink }}>{ev.title}</button>)}</div></>}
        </div>;
      })}
    </div>
  </div>;
}

// Чат з можливістю задавати питання адміністратору
function SupportChat() {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [isSending, setIsSending] = useState(false);
  const [clientId] = useState(getClientId);
  const messagesEndRef = useRef(null);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  const loadMessages = async () => {
    setIsLoading(true);
    try {
      const res = await fetch(`${API_BASE}/chat/messages?client_id=${encodeURIComponent(clientId)}`);
      if (res.ok) {
        const data = await res.json();
        setMessages(data);
      }
    } catch (error) {
      console.error("Помилка завантаження чату:", error);
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    loadMessages();
    const interval = setInterval(loadMessages, 30000);
    return () => clearInterval(interval);
  }, [clientId]);

  const sendQuestion = async () => {
    const question = input.trim();
    if (!question) return;

    setIsSending(true);
    try {
      const res = await fetch(`${API_BASE}/chat/question`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ client_id: clientId, question }),
      });

      if (res.ok) {
        setInput("");
        await loadMessages();
        if (Capacitor.isNativePlatform()) {
          await LocalNotifications.schedule({
            notifications: [{
              id: Date.now(),
              title: "Питання надіслано",
              body: "Адміністратор відповість найближчим часом",
              schedule: { at: new Date(Date.now() + 500) }
            }]
          });
        }
      } else {
        const error = await res.text();
        alert("Помилка надсилання: " + error);
      }
    } catch (error) {
      console.error("Помилка надсилання:", error);
      alert("Не вдалося надіслати питання. Перевірте з'єднання.");
    } finally {
      setIsSending(false);
    }
  };

  const handleKeyPress = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendQuestion();
    }
  };

  return (
    <div className="flex flex-col h-full" style={{ minHeight: "500px" }}>
      <div className="mb-4 rounded-2xl p-4" style={{ background: C.accentSoft }}>
        <div className="flex items-center gap-3">
          <div className="rounded-xl p-2" style={{ background: C.accent }}>
            <MessageCircle size={20} color="#fff" />
          </div>
          <div>
            <h3 className="font-bold" style={{ color: C.accent }}>Служба підтримки</h3>
            <p className="text-sm" style={{ color: C.inkSoft }}>
              Задайте питання щодо доброчесності, декларування або конфлікту інтересів.
              Адміністратор відповість вам у цьому чаті.
            </p>
          </div>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto space-y-3 mb-4 pr-1" style={{ maxHeight: "400px", minHeight: "300px" }}>
        {isLoading ? (
          <div className="flex justify-center py-8">
            <Loader2 className="animate-spin" size={24} color={C.accent} />
          </div>
        ) : messages.length === 0 ? (
          <div className="text-center py-8" style={{ color: C.inkFaint }}>
            <MessageCircle size={40} className="mx-auto mb-3 opacity-30" />
            <p>Історія питань порожня</p>
            <p className="text-sm">Напишіть своє питання, і адміністратор відповість</p>
          </div>
        ) : (
          messages.map((msg) => (
            <div key={msg.id} className="rounded-2xl p-3" style={{ background: msg.answer ? C.greenSoft : C.surface, border: `1px solid ${msg.answer ? C.green : C.line}` }}>
              <div className="flex items-start gap-2">
                <div className="rounded-full p-1.5 shrink-0" style={{ background: C.accentSoft }}>
                  <User size={14} color={C.accent} />
                </div>
                <div className="flex-1">
                  <div className="flex items-center justify-between flex-wrap gap-2">
                    <span className="text-xs font-bold" style={{ color: C.accent }}>Ви</span>
                    <span className="text-xs" style={{ color: C.inkFaint }}>{fmtDateTime(msg.created_at)}</span>
                  </div>
                  <p className="text-sm mt-1 leading-relaxed" style={{ color: C.ink }}>{msg.question}</p>
                </div>
              </div>
              {msg.answer && (
                <div className="flex items-start gap-2 mt-3 pt-3 border-t" style={{ borderColor: C.line }}>
                  <div className="rounded-full p-1.5 shrink-0" style={{ background: C.greenSoft }}>
                    <Bot size={14} color={C.green} />
                  </div>
                  <div className="flex-1">
                    <div className="flex items-center justify-between flex-wrap gap-2">
                      <span className="text-xs font-bold" style={{ color: C.green }}>Адміністратор</span>
                      <span className="text-xs" style={{ color: C.inkFaint }}>{fmtDateTime(msg.answered_at)}</span>
                    </div>
                    <p className="text-sm mt-1 leading-relaxed whitespace-pre-wrap" style={{ color: C.ink }}>{msg.answer}</p>
                  </div>
                </div>
              )}
              {!msg.answer && (
                <div className="mt-2 flex items-center gap-1 text-xs" style={{ color: C.amber }}>
                  <ClockIcon size={12} />
                  <span>Очікує відповіді адміністратора</span>
                </div>
              )}
            </div>
          ))
        )}
        <div ref={messagesEndRef} />
      </div>

      <div className="flex gap-2 mt-2">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyPress={handleKeyPress}
          placeholder="Напишіть ваше питання..."
          className="flex-1 rounded-2xl border p-3 text-sm resize-none"
          style={{ borderColor: C.line, outline: "none", fontFamily: sans }}
          rows={2}
          disabled={isSending}
        />
        <Btn
          variant="primary"
          Icon={Send}
          onClick={sendQuestion}
          disabled={isSending || !input.trim()}
        >
          {isSending ? "..." : ""}
        </Btn>
      </div>
      <p className="text-xs mt-2" style={{ color: C.inkFaint }}>
        💡 Відповідь надається адміністратором у робочий час. Не дублюйте питання.
      </p>
    </div>
  );
}

function Reference({ refs }) {
  const [query, setQuery] = useState("");
  const filtered = refs.filter((r) => `${r.title} ${r.description}`.toLowerCase().includes(query.toLowerCase()));
  return <div className="space-y-4"><div className="relative"><Search className="absolute left-3 top-3" size={17} color={C.inkFaint} /><input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Пошук у довіднику..." className="w-full rounded-2xl border py-2.5 pl-10 pr-3 text-sm outline-none" style={{ borderColor: C.line }} /></div>{filtered.length ? <div className="grid gap-3 sm:grid-cols-2">{filtered.map((r) => <div key={r.id} className="flex flex-col rounded-3xl p-5" style={{ background: C.surface, border: `1px solid ${C.line}` }}><h3 className="text-lg" style={{ fontFamily: serif, color: C.ink }}>{r.title}</h3><p className="mt-2 flex-1 text-sm leading-6" style={{ color: C.inkSoft }}>{r.description}</p>{r.link && <button onClick={() => openLink(r.link)} className="mt-3 inline-flex items-center gap-1 self-start text-sm font-semibold" style={{ color: C.accent }}>Відкрити <ExternalLink size={14} /></button>}</div>)}</div> : <div className="rounded-3xl p-6 text-sm" style={{ background: C.surface, color: C.inkSoft }}>Записів не знайдено.</div>}</div>;
}

export default function App() {
  const [section, setSection] = useState("home");
  const [events, setEvents] = useState([]);
  const [refs, setRefs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState(null);
  const [lastSync, setLastSync] = useState(null);
  const [online, setOnline] = useState(navigator.onLine);
  const [selectedEvent, setSelectedEvent] = useState(null);
  const [reminders, setReminders] = useState(() => loadJson(REMINDERS_KEY, {}));

  const toggleReminder = (id) => {
    setReminders((r) => {
      const next = { ...r, [id]: r[id] === false ? true : false };
      saveJson(REMINDERS_KEY, next);
      scheduleReminders(events, next);
      return next;
    });
  };

  async function load({ silent } = {}) {
    if (!silent) { setLoading(true); setError(null); } else { setRefreshing(true); }
    try {
      const res = await fetch(`${API_BASE}/events`);
      if (!res.ok) throw new Error(`Сервер відповів кодом ${res.status}`);
      const data = await res.json();
      const parsed = data.map((e) => ({ ...e, date: parseDate(e.date) }));
      setEvents(parsed);
      setError(null);
      setLastSync(new Date());
      scheduleReminders(parsed, reminders);
      detectNewEvents(parsed);
    } catch (e) {
      if (!silent) setError(e.message || "Не вдалося з'єднатися з сервером");
    } finally {
      if (!silent) setLoading(false);
      setRefreshing(false);
    }
    try {
      const r = await fetch(`${API_BASE}/reference`);
      if (r.ok) setRefs(await r.json());
    } catch {}
  }

  useEffect(() => {
    async function initPush() {
      if (!Capacitor.isNativePlatform()) return;
      try {
        const perm = await PushNotifications.requestPermissions();
        if (perm.receive !== "granted") return;
        await PushNotifications.register();
        PushNotifications.addListener("registration", async (token) => {
          try {
            await fetch(`${API_BASE}/devices/register`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token: token.value, platform: "android", app_version: "1.0.0" }) });
          } catch (err) { console.error("Register device error", err); }
        });
        PushNotifications.addListener("registrationError", console.error);
        PushNotifications.addListener("pushNotificationReceived", () => load({ silent: true }));
        PushNotifications.addListener("pushNotificationActionPerformed", () => load({ silent: true }));
      } catch (err) { console.error("Push init error", err); }
    }
    initPush();
  }, []);

  useEffect(() => {
    load();
    const onVisible = () => { if (document.visibilityState === "visible") load({ silent: true }); };
    const onOnline = () => { setOnline(true); load({ silent: true }); };
    const onOffline = () => setOnline(false);
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("focus", onVisible);
    window.addEventListener("online", onOnline);
    window.addEventListener("offline", onOffline);
    const timer = setInterval(() => load({ silent: true }), POLL_MS);
    return () => { document.removeEventListener("visibilitychange", onVisible); window.removeEventListener("focus", onVisible); window.removeEventListener("online", onOnline); window.removeEventListener("offline", onOffline); clearInterval(timer); };
  }, []);

  const futureEvents = useMemo(() => events.filter((e) => e.date >= today).sort((a, b) => a.date - b.date), [events]);
  const statusText = online ? (lastSync ? `Оновлено ${lastSync.toLocaleTimeString("uk-UA", { hour: "2-digit", minute: "2-digit" })}` : "Онлайн") : "Офлайн";

  return <div className="min-h-screen pb-20 sm:pb-0" style={{ background: `radial-gradient(circle at top left, rgba(47,126,164,.16), transparent 32%), ${C.bg}`, fontFamily: sans, color: C.ink }}>
    <div className="mx-auto flex max-w-6xl flex-col">
      <header className="sticky top-0 z-30 border-b px-4 py-3 backdrop-blur sm:px-6" style={{ background: "rgba(238,243,247,.86)", borderColor: C.line }}>
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <div className="flex h-11 w-11 items-center justify-center rounded-2xl" style={{ background: `linear-gradient(135deg, ${C.accent}, ${C.accent2})` }}><ShieldCheck size={22} color="#fff" /></div>
            <div><div className="text-lg font-bold leading-tight" style={{ fontFamily: serif }}>Доброчесність</div><div className="flex items-center gap-1.5 text-xs" style={{ color: online ? C.green : C.red }}>{online ? <Wifi size={13} /> : <WifiOff size={13} />} {statusText}</div></div>
          </div>
          <Btn Icon={RefreshCw} onClick={() => load({ silent: true })} disabled={refreshing || loading}>{refreshing ? "Оновлення" : "Оновити"}</Btn>
        </div>
      </header>

      <div className="flex flex-1 gap-5 px-4 py-5 sm:px-6">
        <nav className="hidden w-56 shrink-0 sm:block"><div className="sticky top-24 space-y-1 rounded-3xl p-2" style={{ background: C.surface, border: `1px solid ${C.line}` }}>{NAV.map((n) => <button key={n.id} onClick={() => setSection(n.id)} className="flex w-full items-center gap-3 rounded-2xl px-3 py-3 text-sm font-semibold transition" style={section === n.id ? { background: C.accentSoft, color: C.accent } : { color: C.inkSoft }}><n.Icon size={18} /> {n.label}</button>)}</div></nav>
        <main className="min-w-0 flex-1">
          {loading && <div className="flex items-center gap-2 rounded-3xl p-6 text-sm" style={{ background: C.surface, border: `1px solid ${C.line}`, color: C.inkSoft }}><Loader2 size={18} className="animate-spin" /> Завантаження подій…</div>}
          {!loading && error && <div className="rounded-3xl p-6" style={{ background: C.redSoft, border: `1px solid ${C.red}` }}><div className="flex items-center gap-2 font-semibold" style={{ color: C.red }}><WifiOff size={18} /> Не вдалося завантажити події</div><p className="mt-2 text-sm" style={{ color: C.ink }}>{error}</p><div className="mt-4"><Btn variant="primary" Icon={RotateCcw} onClick={() => load()}>Спробувати ще раз</Btn></div></div>}
          {!loading && !error && <>
            {section === "home" && <Dashboard events={futureEvents} reminders={reminders} toggleReminder={toggleReminder} onAddCalendar={exportCalendar} onOpenEvent={setSelectedEvent} />}
            {section === "calendar" && <CalendarView events={events} onOpenEvent={setSelectedEvent} />}
            {section === "chat" && <SupportChat />}
            {section === "reference" && <Reference refs={refs} />}
          </>}
        </main>
      </div>
    </div>

    <nav className="fixed bottom-0 left-0 right-0 z-40 border-t px-2 py-2 sm:hidden" style={{ background: C.surface, borderColor: C.line }}>
      <div className="grid grid-cols-4 gap-1">{NAV.map((n) => <button key={n.id} onClick={() => setSection(n.id)} className="flex flex-col items-center gap-1 rounded-2xl py-2 text-[11px] font-semibold" style={section === n.id ? { color: C.accent, background: C.accentSoft } : { color: C.inkFaint }}><n.Icon size={18} />{n.label}</button>)}</div>
    </nav>

    <EventModal event={selectedEvent} onClose={() => setSelectedEvent(null)} onReminder={toggleReminder} reminderOn={selectedEvent ? reminders[selectedEvent.id] !== false : false} onCalendar={exportCalendar} />
  </div>;
}