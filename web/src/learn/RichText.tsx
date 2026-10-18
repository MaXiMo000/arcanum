/** Lesson text: `backticks` become code and **double asterisks** bold. Always rendered as text, never as HTML. */
export function RichText({ text }: { text: string }) {
  return (
    <>
      {text.split("`").map((part, i) =>
        i % 2 ? (
          <code key={i}>{part}</code>
        ) : (
          <span key={i}>{part.split("**").map((bit, j) => (j % 2 ? <strong key={j}>{bit}</strong> : bit))}</span>
        ),
      )}
    </>
  );
}
