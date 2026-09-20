export default function EssayBar({ essay }) {
  const line = label(essay);
  if (!line) return null;
  return <p className="shelf-essay">{line}</p>;
}

function label(essay) {
  if (!essay || essay.state === "idle") return "эссе нет";
  const title = essay.title ? `«${essay.title}»` : "без названия";
  if (essay.state === "writing") {
    const bits = [];
    if (essay.passages) bits.push(`${essay.passages} веч.`);
    if (essay.chars) {
      bits.push(`${Math.max(1, Math.round(essay.chars / 1000))} тыс. зн.`);
    }
    return `пишет ${title}${bits.length ? " — " + bits.join(", ") : ""}`;
  }
  if (essay.state === "done") return `закончил ${title}`;
  if (essay.state === "dropped") {
    return `бросил ${title}${essay.closed_why ? " — " + essay.closed_why : ""}`;
  }
  return null;
}
