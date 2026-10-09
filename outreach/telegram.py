"""Send contact drafts to Telegram: LinkedIn notes to copy, emails behind a Send button."""
import html


async def send_contact_drafts(notifier, saved: list[dict], drafts: dict) -> None:
    e = html.escape
    for i, item in enumerate(saved):
        c, d = item["contact"], drafts.get(i)
        where = f" at {e(c.company)}" if c.company else ""
        head = f"👤 <b>{e(c.name)}</b>, {e(c.title or 'title unknown')}{where}"
        if item["linkedin_msg_id"] and d:
            await notifier.send_text(
                f"{head}\nLinkedIn note (tap to copy, then send it yourself):\n<code>{e(d.linkedin_note)}</code>",
                [[{"text": "Open LinkedIn profile", "url": c.linkedin_url},
                  {"text": "I sent it", "callback_data": f"lisent:{item['linkedin_msg_id']}"}]])
        if item["email_msg_id"] and d:
            await notifier.send_text(
                f"{head}\n📧 Email to {e(c.email)}\n<b>Subject:</b> {e(d.email_subject)}\n\n{e(d.email_body)}",
                email_buttons(item["email_msg_id"]))
        if not d:
            await notifier.send_text(f"{head}\nNo draft for this contact.")


def email_buttons(msg_id: int) -> list:
    return [[{"text": "📧 Send email", "callback_data": f"email:{msg_id}"},
             {"text": "Don't send", "callback_data": f"noemail:{msg_id}"}]]
