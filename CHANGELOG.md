# Changelog

What changed in each release, written for the person installing it. The commit
history has the reasoning; this has the effect.

The release workflow reads this file: the section matching the tag becomes the
release notes on GitHub and the text the app shows when an update is waiting.
A version with no section here falls back to the commit subjects since the last
tag, so a release is never published with nothing said about it.

## 0.24.0

A polish and safety release on the way to 1.0.

- **Other websites cannot reach CV Studio through your browser.** The app
  answers only on your own machine, but a page you had open elsewhere could
  still send it requests, and since 0.23.0 knew where to find it: the Save
  to CV Studio button's address is fixed. Requests that do not name this
  machine (a site pointing its own domain at it) and writes from any page
  that is not CV Studio's own are now refused. Scripts and your AI client
  work as before.
- **Next actions** replaces Next up on the applications list: one row per
  thing to do, most urgent first, each with its button. An interview coming
  (*Prepare*), a follow-up late (*Write follow-up*, drafted to the person you
  last wrote to), an offer to answer (*Reply to the offer*), a thank-you the
  day after an interview, an interview with no outcome yet, a draft left
  for days. Beside it, this week, with three counts: interviews this week,
  sent this week, waiting on a reply.
- **Interview rounds.** An application keeps its interviews as rounds (a
  screen, a technical round, team fit, a final), each with its time and zone,
  who you meet, and whether you passed. The next one is open to edit on the
  application; a round you did not pass offers to mark the application
  Rejected after interview. The next undecided round is still the one
  interview time the calendar, reminders and AI clients read, and setting
  that time (from the calendar or your AI client) moves the round. Rounds
  are in the CSV export.
- **Words split across lines.** Justified CVs hyphenate long words at the
  end of a line, so an ATS reads "Post-" and "greSQL" and a keyword search
  misses both. The ATS check now says so, with a Fix that keeps the text
  justified without hyphenation.
- **New CVs are ATS-safe from the start.** The starter CV (and the sample)
  print contact details as plain text instead of icon glyphs, the full
  LinkedIn and GitHub links instead of usernames, and the phone with its
  country code (+33 6 12 34 56 78, not 06 12 34 56 78), and they never
  split a word across lines. The ATS check has nothing to flag on them;
  Design turns any of it back.
- **Round five of the review:**
  - "Interviews in 7 days" counts the same way everywhere (today and the
    next seven days), so the sidebar and the week's count agree.
  - Next actions puts an interview in the next three days first; an offer
    left more than a month asks whether you took it, with *Accepted* and
    *Declined by me* buttons.
  - An application in interviews with no round booked shows "Book the next
    round" as its next step; the email sheet leaves the follow-up date alone
    when an interview is booked.
  - The month says what ended ("1 rejected, 1 ghosted"); the funnel chart
    fits the window, so every final stage is in view.
  - The base CV no longer offers to be linked to an application; Settings'
    Done is filled like Design's; the interview times on Journeys are calm
    in dark mode; French names the hiring manager "Manager du poste".
- **Round six of the review:**
  - An offer more than a month old reads "Accepted or declined?" in the
    table too, and sits with the other offers in Next actions.
  - An application silent for six weeks asks *Follow up or let it go*, with
    *Mark ghosted* beside *Write follow-up*.
  - With a round booked, the email to write is *Confirm the interview*
    (date, time and zone filled in), in all four languages.
  - Funnel: one base for every share (of sent, in the chips and on hover;
    none on drafts); a step with fewer than ten behind it is greyed "too
    few to say" and never called the lowest.
  - Search: "Show all N in Applications" opens the table with the same
    words; the table search now matches word by word, accents aside.
  - The calendar tells two applications at one company apart with a word
    of the role; "Company → Interviewing"; the clocks say they show the
    interview's time; the month heatmap counts interviews double and
    rejections not at all.
  - A letter whose application was sent is dated the day it was sent, not
    the day it is exported. The editor's word count is the ATS check's.
  - Saving from the browser drops "(H/F)", "m/w/d" and the like from the
    role. The saved view is "Open, no letter" and counts open applications.
  - Sample: offers come after the final round and are answered within a
    fortnight; interviews fall on weekdays. Smaller: rounds sum without a
    total, "Last email 16 Sept", the API example's line break, no "Check
    now" outside the desktop app, French wording.
- **Round seven of the review:**
  - The applications table sorts by any column; by default what needs you
    first (offers, late follow-ups, interviews coming, follow-ups coming,
    rounds to book), with an arrow on the column in use.
  - The calendar keeps the rounds already held, muted, where they happened;
    two in the same hour sit side by side in the week. The week's all-day
    row shows offers, moves to interviews and what was sent or ended. The
    month heatmap marks interviews in the days of the next month too.
  - "Reply to offer · 3 d ago" says it counts since the offer, and turns red
    only after five days.
  - New CVs and the sample leave out "4 years 7 months" under each date;
    the outline names a paragraph by its first sentence.
  - The base CV card fits above the status bar on a shorter window.
  - French: interview rounds are "étapes"; "Marquer sans nouvelles" matches
    the status it sets; "19 sem. de silence".
- **Polish after the review:**
  - The status bar says something on every screen: applications and how
    many are open on the funnel and calendar, saved or not in a letter.
  - Calendar month: follow-ups read "↩ Company", a move to interviews
    "Company · invited", with the full line on hover; a crowded day shows
    two and a count, so its sent and ended line stays in view.
  - The funnel's stage list shows its stage colour again, aligned with the
    other lists. The dark heatmap's quiet days stand apart from the card.
  - The ATS check's "What it reads" joins the PDF's lines into paragraphs.
  - Next actions lets a long role take a second line; the table's next
    step may wrap rather than cut. The base CV tag no longer covers the
    name on the page. French: the Journeys axis leaves room for
    "Aujourd'hui".
  - Sample letters match the CV (four years at Northwind).
- **Round four of the review:**
  - The email a person's row suggests fits the moment: a thank-you after a
    round in the last two days (to whoever was in it), a plain follow-up
    while the next round is booked, a question about next steps only when
    nothing is.
  - Reminders are offered, once, the first time you give an interview a time.
  - "Overdue" everywhere for a follow-up past its date; the calendar's
    overdue count opens those applications; the week's interview count uses
    the same seven days as the sidebar.
  - The list's next step for an offer is short enough to read ("Reply to
    offer · 78 d"); the funnel's shares past the first split are of what was
    sent, as in the steps above it; "Offer pending" beside "Offers".
  - The sample CV is left-aligned, with no wide gaps between words.
- **Round three of the review:**
  - An interview this week with no tailored CV says so on the application,
    with *Tailor a CV*; the calendar's checklist offers the same.
  - An offer's next step in the list is to answer it; the offer email
    template only shows on an offer.
  - The interview round editor stacks its fields, so dates and French labels
    are no longer cut off.
  - Sheets float below the top bar like Settings and search; in dark mode the
    page behind them goes further back.
  - Search names a document by its application ("CV · Qwant (FR)") and shows
    the matching line without its YAML key.
  - AI client marks in the top bar are dimmed until connected; the calendar's
    Prepare is the orange primary like everywhere else.
- **Round two of the review:**
  - The applications table shows the *next step* (the interview coming, or
    the follow-up, late in red) instead of a follow-up column of dashes.
  - An offer reply goes to the hiring manager or recruiter, never the person
    who referred you; their row offers a follow-up instead.
  - Save to CV Studio warns when it caught only a few dozen words of the
    posting, instead of ticking it off.
  - The editor's Save is only orange when there is something to save.
  - An offer left unanswered for over a week turns red in Next actions;
    the week's counts show the follow-ups due this week; Fit shows its value
    (3/5); "Interview soon" says its seven days; "Declined" says "by me".
  - Calendar: one interview mark everywhere, clearer busy steps in the dark,
    Coming up lists only what is ahead, month chips name the company (the
    role is in the tooltip), and the "ended" count says which ones ended.
  - Search describes a document by the application it is for; the funnel's
    root stage no longer opens a list of everything; late is a clearer red
    in dark mode; the Momentum card says it counts applications that reached
    interview.
- **A second, harsher review of every screen**, asking of each number and
  block whether it helps the search:
  - Offer is green everywhere (it was ochre in the list and green on the
    funnel), and in dark mode Rejected no longer looks like Interviewing.
  - One vocabulary: the funnel and calendar use the status names ("Offer",
    "Interviewing", "Awaiting reply") instead of "Deciding", "live",
    "Waiting" or "closed"; "No reply" says it means two weeks.
  - The email draft fits the application: an offer gets a reply asking for
    the details in writing and a deadline, an interview a few days old a
    thank-you, an older one a question about next steps. It shows the
    address it goes to, and a follow-up moved a week never lands on a
    weekend.
  - The CV editor's button says Save (it saved all along), and the status
    bar says "all saved" or "unsaved changes" instead of render times.
  - Less noise: no seconds on the calendar countdown, no database file name
    or long paths in the status bar, no "1pp" on every document (a page count
    shows only past one page), no setting counts in Design, a link icon
    instead of an unexplained dot on linked documents, CV + letter in the
    list instead of file names, and "3 drafts not sent yet" on the funnel
    instead of a share of everything tracked.
  - New application asks for the location, and suggests the same places as
    Found on; New document starts from your base CV; the cover letter says
    when it is short; Settings exports CSV as well as JSON; the search
    shortcuts lead to follow-ups due.
  - Journeys keeps today in the middle, so labels no longer sit on the
    Today line; calendar entries name the role when a company has two
    applications; French says Londres, Adéquation and Déclinée par moi.
- **A polish pass over every screen**, after a review of each one:
  - Nothing that matters is cut off any more: the next interview, the
    overdue follow-ups (their "Show them" link was hidden in French), the
    editor's title, a person's last contact, the longer statuses.
  - The editor always sits under Documents, and under Applications only
    when the document was written for one; its title is the document's name.
  - The application panel: the interview time and notes look like the other
    fields, empty CV and letter slots are short cards, the Fit bars read
    clearly and the source shows its own icon.
  - The ATS check stays inside the window, with its buttons at the bottom.
  - The funnel's highlighted step says what it is ("to offer · lowest
    step"), not "biggest drop" beside the share that got through.
  - Search counts agree ("6 of 22 shown").
  - In French a job posting is an "annonce", no longer an "offre" like a job
    offer (Spanish: "anuncio"); decimals take a comma; a few awkward phrases
    are rewritten.
  - The Save to CV Studio window uses the app's font and fields, in the same
    order as New application, and is as tall as what it shows.
  - Smaller: the countdown no longer ticks seconds, long YAML lines wrap,
    New document says what it will make, every checkbox takes the accent,
    and in dark mode logos and floating panels keep a visible edge.
- **Save to CV Studio reads Indeed, LinkedIn and Glassdoor**, including
  their search pages: it takes the job you have selected in the side panel
  (its role, company, place and full description) and links to that job,
  not to the search. Its name now starts with 💼, since a browser gives a
  bookmark of this kind no icon. Drag the button from Settings › Browser to
  your bookmarks bar again to get both.
- **The sample data has people**: recruiters, hiring managers, interviewers
  and whoever referred you, on most applications that got that far.
- **Hybrid, Remote and On-site** on a posting's tag are translated.
- **A log, for when something goes wrong.** The desktop app now keeps what
  the renderer notes while running, errors with their details included, in a
  small file beside your settings (never over a megabyte). Settings › About
  › Open the log folder finds it, to attach to a report.
- **The CSV export includes people**, as "Name (Role) <email>" in one column.
- **The README** describes search, People, Export both, the browser button,
  backups and the trash.

## 0.23.0

- **Search everything, from anywhere.** Ctrl K (⌘K on a Mac), or the
  magnifier in the top bar. Before you type it offers what you opened last
  and the places you go most; as you type it finds applications by company
  and role, documents by name and by what is written in them, and the words
  inside postings and notes, each with the match highlighted. Arrows and
  Enter to open, and a place like Settings › Notifications is a few letters
  away. Accents and capitals do not matter. A search field in the top bar
  opens it too.
- **The CV and the letter as one file.** Export both… on an application
  gives one PDF, the CV then the letter, for forms that take a single file,
  or a zip of separate files (with the posting, if you like). It is named
  for you, the company and the role, is rendered from what is saved, and
  can mark a draft as applied today.
- **Save a job from your browser.** Settings › Browser has a Save to CV
  Studio button to drag to your bookmarks bar. On a job page it opens a
  small CV Studio window with the role, the company, the place, the salary
  and the posting (headings and lists kept) read from the page; you check
  them and save. On a page that does not describe its job, it takes the text
  you selected. A link you already track offers to save the posting to that
  application instead of a duplicate. Everything goes straight to the app on
  your computer, which has to be running (the tray is enough).
- **People, and what to write them.** Each application has a People card:
  the recruiter, the hiring manager, whoever referred you, with their email
  and when you last wrote. A follow-up, a thank-you after an interview or a
  question about next steps is drafted from the application, in its
  language, and opens in your own mail app; sending a follow-up can move the
  reminder a week on. Your AI client can add people from an email thread
  (save_person). The single Contact field becomes the first person.
- **Smaller fixes.** The fields under "Company, role and the rest" are
  announced by their labels; the words of your CV in "Changed from the
  base" are no longer run through the translator; the change count there is
  translated; the selected row in the narrowed list is readable in dark
  mode.

## 0.22.0

- **The editor no longer freezes in French, Spanish or Portuguese.** In
  0.20.0 and 0.21.0, opening a CV with the app in one of those languages
  could lock the window: the translator kept rewriting a page count ("1
  page") back and forth. Fixed.
- **Quick with hundreds of applications.** With 500 in the list, the
  calendar opened in over 200 ms and opening, closing or searching took
  100 to 220; all of it now takes under 70. Dates are formatted by
  formatters made once instead of thousands of times, choosing an
  application no longer rebuilds the whole table, and rows off screen are
  laid out only when you scroll to them.
- **Easier to read, and to use without a mouse.** Coloured text (the
  calendar's chips, overdue follow-ups, offers, the funnel's tooltip, the
  Next up seconds) now meets the WCAG AA contrast bar in light and dark
  mode. Pressing Tab reaches every control, including
  the rows of the calendar's journeys, and always shows where you are;
  Enter and Space work on anything that acts as a button. Settings are
  announced by their titles to a screen reader, the mini month says how
  much is planned each day, and the Next up countdown reads as one time.
  With notifications off, their settings can no longer be changed from the
  keyboard either.
- **Every screen in your language.** The last English left on the French,
  Spanish and Portuguese screens is translated: the assistant's status,
  the letterhead note, the funnel, the application's documents and more,
  and the messages that say what went wrong: a failed import, a render
  error's hint, a name already taken. A phrase with two plurals in it now
  agrees in both words ("2 choses prévues", not "2 choses prévue(s)").
  Date fields now show dates the way your language writes them, whatever
  language the system itself is in.

- **Backups, every day.** CV Studio zips your workspace once a day into its
  own data folder (not the workspace, which you may sync elsewhere) and keeps
  the last fourteen: the CVs, letters, applications, the photo and the logos,
  with the database copied consistently even mid-write. Settings › Workspace
  has *Back up now* and *Restore…*, and a restore backs up what is there
  first, so it can be undone the same way.
- **Nothing is lost to a click.** Deleting an application moves it to a
  trash kept for thirty days, and the toast that says so has *Undo*, which
  brings it back with its history and documents. Documents can now be
  renamed and deleted from the app: the *⋯* beside the title in the editor,
  or on a card on Documents. A renamed document takes its application, the
  base CV setting, its translations and the letters that name it along; a
  deleted one goes to a `.trash` folder in the workspace. The base CV, and a
  document other languages are translated from, cannot be deleted until
  that is changed.
- **Reminders come with the window closed.** With notifications on, closing
  the window leaves CV Studio in the tray (the menu bar on a Mac), where
  *Open CV Studio* and *Quit CV Studio* live, so a 9:00 follow-up still
  arrives. *Keep running when the window is closed* turns that off, and
  *Open at login* starts it in the tray when you log in. Both are in
  Settings › Notifications.
- **One CV Studio at a time.** Opening the app while it is already running
  brings the open window forward instead of starting a second copy on the
  same files.
- **Your settings survive a restart.** Theme, accent, language, time zone,
  notifications and the rest were kept in the window's own storage, which the
  desktop app lost at every launch (it serves the window from a new port each
  time, and that storage belongs to the port). They are now a file beside the
  app, handed to the window as it opens, so the theme is right before the
  first frame. Whatever this window had kept is moved into it on the first
  launch after the update. The record of reminders already sent moved with
  them, so a restart no longer sends one twice.
- **Your AI client can save the posting later.** `update_job_tracking` now
  takes the posting's link, text, location and where it was found, so "save
  the posting for Mistral AI" works after the application exists. A posting
  already saved is not replaced unless you asked for that.
- **Reminders, as system notifications (opt-in).** Settings › Notifications
  turns them on. Before an interview (10 minutes, 1 hour, both, or the day
  before and 1 hour before), with the time in yours and in theirs; and once a
  day, at the hour you pick, one notification for the follow-ups due that day
  and the ones already late. Each is sent once, and opening the app half an
  hour before an interview still gets you the reminder. *Send a test* shows
  where they appear. They come while the app is open, even in the background.
  This replaces the old once-a-day "applications need attention"
  notification, which was always on.
- **The posting, in one place.** On an application, the posting is no longer
  a tile among the documents pointing at the text beside it. It is one card:
  its header says how long it is and holds *Open on LinkedIn ↗* (or the
  site's name, for a referral or a recruiter's link) and *Edit*; the text is
  below. Not saved yet, the card is the paste box, with *Save the posting*
  and the sentence to ask your AI client instead. No link saved: *Add the
  link*. Documents are now only what you send, the CV and the letter.

## 0.21.0

- **Next up, on the home screen.** Above the applications list, a strip says
  what is coming: the next interview with a live countdown, your time and
  theirs, and Prepare and Add to calendar; the follow-ups that are late, with
  their logos and how late the oldest is (*Show them* filters the list to
  them); and this week in seven days, interviews as diamonds and follow-ups as
  rings, each day opening the calendar's week. When an interview is less than
  three hours away it takes the whole strip, with a checklist of what is ready.
  With only follow-ups due it is one slim line, and with nothing to act on it
  is not there at all. It shows on the full list, and steps aside while you
  filter, search or have an application open.
- **Search boxes and other placeholders are translated.** "Search
  applications", the posting box and the letter's fields now follow the app's
  language like the rest of the interface.

## 0.20.0

- **A little world on the time zone setting.** Settings › Language & region
  now draws the world in dots, shaded where it is night right now, with your
  zone's band and your city pinned beside its time. Cities where you have an
  interview coming up are marked and linked to yours. Hover to see the nearest
  city and its offset; click to switch to it.
- **Try it before you set it up.** The welcome screen has a second way in,
  under "Skip setup": *Try it with sample data* opens the app on the made-up
  workspace straight away, and your own folder is never touched. The welcome
  is a little tighter on laptop screens so it all fits without scrolling.
- **Sample companies with their logos.** Every company in the sample data now
  has its mark, drawn on a tile in its brand colour (from Simple Icons, which
  ships with the app, so nothing is fetched). The few that had no mark
  available were swapped for companies that do, among them Revolut, Figma,
  Adyen, Zalando, Klarna and Booking.com, with their own cities and zones.
- **A pass over every screen.**
  - The editor's top bar fits a smaller window: the language switch drops
    to flags, and *ATS check*, *Export PDF…* and *Render* no longer wrap or
    fall off the edge.
  - In the documents rail, the base CV leads its group with its translations
    tucked under it by language, instead of four rows all called "my-cv".
  - Time zones read as offsets everywhere you pick one: "São Paulo · UTC−3"
    rather than "São Paulo · America". Changing yours brings you back to the
    settings you were in.
  - The calendar's month puts *Coming up* under the grid on narrower windows,
    so events stay readable.
  - An application's document cards keep their buttons on one line and keep
    a sensible size on narrow windows. The interview row no longer stretches
    across the whole page.
  - Company logos on a dark tile get a faint ring in dark mode so they don't
    melt into the background.
  - French, Spanish and Portuguese say "1 envoyée" and "3 envoyées" instead
    of "envoyée(s)".
  - Smaller things: theme names are capitalised in Settings; a letter dated
    *Today* no longer shows an empty date field; an AI client that isn't set
    up no longer promises "two steps" above three; no stray space before
    "updated today" on the base CV card.

## 0.19.0

- **More light in the funnel.** The dots flowing to the stages still in play
  are a stream now: about one per application waiting there, spread across
  the band, each with its own size, glow and pace.
- **A Calendar tab.** Its overview opens on your next interview: who and
  which role, a live countdown to the second, two clocks (your time and
  theirs, with how far apart they are), what is ready for it (tailored CV,
  saved posting, notes), and buttons to prepare or add it to your calendar.
  Beside it, the month tinted by how busy each day was, with interview days
  marked. Below, **Journeys**: every application still in play as a lane
  across eight weeks, blue while you wait, ochre once interviewing, green at
  an offer, with a Today line; ahead of it, interviews glow with both times
  and follow-ups sit as rings, red and pulsing when overdue.
- **Month and Week views.** Month shows interviews, follow-ups, replies and
  offers on their days, with a quiet count of what was sent and closed, and
  Coming up for the next seven days, overdue first. Drag an interview or a
  follow-up to another day to move it. Week puts interviews at your hour,
  the other zone's time inside, and opens a card with both times and your
  notes.
- **Export .ics** puts interviews (at the right moment, whatever the zone)
  and follow-ups into any calendar app; "Add to my calendar" does one. The
  app itself still makes no network calls.
- **AI clients get `calendar`**: interviews and follow-ups ahead, each
  interview with its moment in UTC, and the .ics on request, so a client with
  a calendar connector can put them in your calendar.

- **Languages are opt-in.** Settings → Language & region → CVs in more than
  one language. Off, the app never mentions languages: no tabs, flags,
  filters or language questions when you tailor. In their place, one setting
  for the language your CVs print in (dates, month names, "present"). It is
  on by itself once a CV has a translation, and turning it off deletes
  nothing.
- **The base CV card in the sidebar** shows the page standing on a small
  stage, with its name, a flag for each language it comes in, how many CVs
  were tailored from it, and Open beside Design and Change.

## 0.18.0

- **The funnel chart follows the theme.** In light mode it sits on a white
  card like everything else, in the light palette; in dark mode it keeps the
  dark stage. Labels and the hover card adjust with it.
- **Sample data, to see the app in use.** Settings → Workspace → Open sample
  data switches to a separate folder, made fresh each time, with 64
  applications over five months in every status (follow-ups due, interviews
  coming up, offers to decide on), postings, notes, five tailored CVs, four
  cover letters, and the base CV in French, Spanish and Brazilian Portuguese.
  A pill in the top bar says you are looking at it and takes you back. Your
  own workspace is never written to, and a restart always opens it.
- **CV Studio in French, Spanish and Brazilian Portuguese.** The app speaks
  your computer's language, or the one you pick in Settings → Language &
  region. Screens, settings, the funnel's readings, messages and dialogs are
  translated, and dates and month names follow. Your CVs, letters, postings
  and notes are never touched: each document keeps its own language.
- **Interviews in their time zone and yours.** An application has an
  Interview field: the time as the invitation gave it, and the zone it is in,
  guessed from where the job is (London, São Paulo, New York…). Below it, the
  time for you: "Fri 25 Sep, 11:00 your time · 10:00 in London". Reminders
  use the real moment. AI clients can pass the zone with `interview_tz` when
  an invitation gives the employer's time. Settings → Language & region sets
  your own zone; it follows your computer by default.
- **Flags beside languages.** France, Spain, Brazil for Portuguese and the US
  for English, drawn rather than emoji so they look the same on Windows, on
  the base CV's language tabs, the Documents filter, the application's
  language and every language chip. The code stays beside the flag.

## 0.17.0

- **The funnel, rebuilt.** It opens on five numbers across the top, sent,
  heard back, interviewed, offers and accepted, counting up, with the rate
  between each pair and the step that loses the most marked. The chart sits
  on a dark stage in both themes, sweeps in column by column, and every stage
  has a label that never covers a number. Small lights travel along the paths
  to the stages still in play (awaiting a reply, still interviewing, deciding
  on an offer), so what can still change is what moves; an accepted offer
  glows. Hover a stage and its whole path lights up, with who is in it; click
  it and the list beside the chart shows them. Changing the range moves the
  chart from the old shape to the new one rather than redrawing it.
- **Beside and under the chart:** applications sent each week, with the weeks
  interviews happened; the applications still in play and what is next for
  each; which sources turn into interviews, by board; how many days answers
  take, each one a dot, with the ones that never came set apart; and up to
  three plain readings of all that, each shown only when the numbers support
  it. With reduced motion turned on, nothing moves.
- **Median reply time no longer counts being ghosted as a reply.** Marking an
  application ghosted is you giving up on an answer, and the day you did was
  being counted as the day they answered.
- **A third smaller.** The download is about 75 MB instead of 110 MB. The
  Chinese, Japanese and Korean fonts, two fifths of the fonts the app carried,
  are fetched the first time a CV needs one: when you add one of those
  languages, or open a CV written in one. Chinese is 22 MB, Japanese and
  Korean 11 and 12, once each, into CV Studio's folder in your app data, and
  checked against the file they replace before they are used. Without a
  connection the page still prints, in a font your computer has. An image
  library nothing used is gone too.

## 0.16.0

- **Cover letters are letters now.** They used to be CVs in disguise: the
  subject a section heading with a rule under it, the paragraphs entries, no
  date and no signature. A letter is now a Markdown file (a short header, and
  the letter below it as you would type it in an email) that CV Studio lays
  out itself: the letterhead of the CV it goes with, in that CV's font, colour
  and margins, then the place and date, a subject line, the letter, and your
  name. Change the CV's design and its letters follow. Letters you already
  had are converted the first time the app opens, and the old file is kept
  beside the new one as `.yaml.bak`.
- **Write on the letter itself.** The letter opens as its page, at its real
  size and in its real font, and you click and type. Select text for bold,
  italic, a link or a list, which is everything a letter prints. Beside it:
  the word count against about 350, whether it fits on one page, the CV it
  looks like, its language, where it is written from, the date, and an
  optional recipient. **PDF** shows the exact render; **Markdown** shows the
  file. **Export** gives the PDF, a Word file for recruiters who ask for one,
  or plain text for an application form's box.
- **Write one, from the application.** It makes the letter with everything
  but the words filled in: the subject, greeting and closing in the posting's
  language (« Madame, Monsieur, » in French), today's date, the look of the
  application's CV. It shows the sentence to ask your AI client if you want it
  to draft the paragraphs; AI clients get `create_letter` and `write_letter`.
- **An application reads as a page.** The company's logo, the role, where,
  and its status at the top; status, follow-up, fit, where you found it and
  its language in one row; the CV, the cover letter and the posting as cards
  with their pages; and the posting itself with its headings and lists, with
  the place, salary, travel and visa sponsorship pulled up as chips. A posting
  pasted as plain text gets its headings and lists found too.
- **Where you found it is chosen, not typed**: a menu of job boards with their
  logos, then the company's careers page, a referral, a recruiter, or
  anything else.
- **A CV in more than one language.** On Documents, **+ Add a language** on
  the base CV makes a copy in any of the 22 languages RenderCV prints, saved
  beside it (`my-cv.fr.yaml`) and linked to it. The copy already has its
  dates, month names and common section titles in the new language (a French
  one says "aujourd'hui" rather than "présent"), and your name, contact
  details, links, company names and design are left as they are. The text
  stays in the original language until you, or your AI client, translate it:
  the app shows the sentence to ask your client, ready to copy. Nothing in
  the app starts an AI client; you ask it, in Claude Desktop, ChatGPT,
  Hermes or Mistral Vibe.
- **Translations say what they are missing.** When the English CV changes,
  its French version shows what changed and not yet carried over, English
  then and now beside the French, in the editor and on its tab on Documents.
  **Mark as done** once it is up to date. Nothing is overwritten for you.
- **One design for every language.** A theme, margin or font change on the
  source reaches its translations, so they print alike. **Design** on a
  translation opens the source's.
- **Applications have a language**, read from the posting when it is added
  and changeable on the application. **Tailor a CV** copies the base CV in
  that language, and offers to add the language first when there is none.
- **Switch languages in the editor** from the bar at the top, and filter
  Documents by language once there is more than one.
- **A photo, if you want one.** Design has a **Photo** group: drop in a
  picture, crop it to a square, and it is saved small (600 × 600) in your CV
  Studio folder, with the original not kept. Each CV shows it or not, and CVs
  you tailor start the way their source is. A **Photo** chip in the editor
  turns it off for one application, and says so when the application is in
  the UK, the US or Ireland, where recruiters usually ask for CVs without
  one; the ATS check says the same. Its size and side of the page, which
  RenderCV keeps under Header, are in the Photo group now. Removing the photo
  takes it off every CV that showed it, so none of them stops rendering.
- Sheets opened from Design or Settings now open on top of them rather than
  behind.
- **A consistency pass.** Buttons in sheets are the same shape and height as
  the ones on the page, and every screen header's buttons are one height.
  A selected option is a white pill everywhere, so ochre only ever marks the
  one action on a screen; Settings → AI clients no longer shows four ochre
  buttons at once. Form labels read "Name" and "Headline" rather than the
  YAML keys. Applications list their CVs by name, without `.yaml`. The
  funnel's first stage says "All applications". A translated CV is listed by
  its name with its language, not as `my-cv.fr`, and only documents in
  another language than the base CV carry a language tag. The document
  count in the status bar keeps up with Documents.
- AI clients get `add_language`, `translation_status` and
  `mark_translation_current`, and `add_job` takes the posting's language.

## 0.15.0

- **Design is a screen, and it keeps the page in view.** It opens under the
  title bar with the groups RenderCV defines down the side (Theme, Page,
  Colors, Typography, Header, Section titles, Sections, Entries, Links,
  Templates), every setting of the chosen group in the middle, and your pages
  on the right as they re-render. A dot marks each setting your CV changes
  from the theme's default, with a Reset beside it. From the base CV card,
  **Design** opens the base CV's, which every tailored copy starts from.
- **The theme tiles are your CV.** Each one is your document rendered in that
  theme, with its page count, instead of a sketch of the theme.
- **Every design setting takes effect.** A setting in a group your file had
  never written, such as the header's alignment on a starter CV, used to be
  dropped without a word. Only what you change is written now, and template
  text keeps its line breaks.
- **A setup on first launch, over the whole window.** It opens on the mark
  and the name, animated, with what the app is and where your files live.
  Then four steps down a rail: the CV you already have, your name and contact
  details at the top of it, the theme and paper size it prints in, and an AI
  client to connect. Beside each is your own page, rendered as you type and as
  you switch themes, not a sample, and every theme tile is your CV in that
  theme. Every step can be skipped, and **Finish later** keeps what you
  entered. It comes back on the next launch if the base CV is still the
  placeholder, and **Settings → Workspace → Run setup again** brings it back
  any time. With reduced motion turned on, nothing moves.
- **Start from the CV you already have.** Setup imports a PDF of your CV,
  your LinkedIn profile saved as PDF, or LinkedIn's data archive (the .zip it
  emails you), and shows what it read (contact details, roles, education,
  skills, languages) before anything is saved, beside the result rendered.
  Anything it was unsure of is listed to check, such as a phone number with
  no country code. The file is read on your machine and nothing is uploaded;
  CV Studio never signs in to LinkedIn.
- **Documents shows your documents.** The base CV sits at the top as its
  whole first page, readable, beside how many CVs were tailored from it and
  how many went out with an application. Every other CV and letter is a card
  with its own rendered page, the application it was written for and its
  status. Pages missing or older than their file render one at a time while
  the screen is open.
- **Import on Documents** does the same for any CV later: it becomes a new
  document in your base CV's design, and the base is left alone.
- **The base CV card shows the base CV.** A snapshot of its first page, as
  actually rendered, sits in the card on Applications and on Documents, and
  clicking it opens the CV. It updates when you or a model change the file.
- **No CV says "Last updated in…" any more.** RenderCV prints it at the top
  of page one unless a file turns it off, and a design block rewritten by a
  model dropped the setting, so it crept back. Every render now turns it off,
  whatever the file says, and the switch is gone from Design. Your files are
  not changed.
- **The base CV says it is the base.** Open it and a **Base CV** chip sits
  where a tailored copy says what it came from, with how many CVs have been
  tailored from it; the document list tags it too.
- **ATS check.** Reads the CV's PDF the way an applicant tracking system
  does and shows you the text it gets, beside what went wrong: contact icons
  that extract as junk characters, a LinkedIn shown as a bare username,
  headings a parser will not recognise, entries missing or out of order in
  the text layer, roles without dates. The two commonest problems have a
  one-click fix. Against an application's saved posting, or one you paste,
  it lists the keywords the posting asks for and which ones the CV uses. From
  the editor's **ATS check** button, or from an application. Claude can run
  it too, with `ats_check`.
- **Applications Claude adds come with the company's logo.** Given the
  company's website, `add_job` fetches its icon from that site and shows it on
  the row; a second role at the same company reuses it. The request goes to
  the company's own site and nowhere else, and a logo that cannot be found
  never stops the application being added. `set_company_logo` takes a website
  as well as an image on disk.
- **Where each posting was found.** LinkedIn, Indeed, Glassdoor, Greenhouse,
  Welcome to the Jungle and the rest show their mark beside the role, worked
  out from the source or the link. Boards with no published mark get their
  initials.
- **What a tailored CV changed from the base, on the application.** Under the
  documents, every field that differs from the CV it was copied from, what it
  said before and what it says now, and whether the design changed.
- **Open the posting opens it.** The desktop app ignored links that open a new
  window, so the link to a posting did nothing. It now opens in your browser.
- **Two renders at once no longer break each other.** Rendering changed the
  working folder of the whole app, so a render that overlapped another could
  fail with an error about a file that was really there.

## 0.14.0

- **One workbench instead of two.** Only the title bar is dark now. The
  sidebars and the footer sit on the same paper as the work, so the window no
  longer reads as a dark frame bolted around a light page.
- **The title bar stays the same shape on every screen.** It carries the new
  mark, the three tabs and the AI clients, and nothing else. Search and New
  application live in the Applications header, the date range lives on the
  Funnel, and the editor keeps the tabs, with a crumb back to the list you
  came from.
- **The AI clients show their logos.** Connected ones carry a green dot on
  the corner; ones you have not set up are faded.
- **Ochre means one thing at a time.** It marks the primary button, the
  selected row and a follow-up that is due. An application with no CV says
  "Not tailored" quietly and offers **Tailor a CV** when you are on the row,
  rather than every row shouting it in orange.
- **An open application gets the room.** The table narrows to a list of
  companies and roles on the left, and the record takes the rest of the
  window, instead of the other way round.
- **The base CV is a card at the foot of the filters**, not a band across
  the top of the list.
- **The Funnel reads at a glance.** The rates are four tiles across the top,
  the bands are flat colour, and clicking a stage lists its applications
  beside the chart instead of underneath it.
- **Settings opens over the app** rather than replacing it.

## 0.13.0

- **Your documents have a screen.** Until now the only list of them lived
  inside the editor, so finding a CV meant already having one open — and a CV
  written for nothing, a master copy or an old version, was reachable from
  nowhere at all. **Documents** sits beside Applications and lists everything:
  the ones written for an application, each showing which one, and the ones
  written for nothing.
- **The base CV can be seen.** It was a thin band the colour of the table
  header, its name set smaller than the company names below it, with its
  buttons a screen-width away at the other edge. It is now its own surface,
  the name outranks the rows it is the parent of, and Open sits next to it.
- **Back knows where you came from.** Leaving a document returns you to the
  application it was written for, as before — or to Documents, if that is
  where you opened it and it belongs to no application. The button says which.
- **Jobs is called Applications**, in the tab, the search box, the footer and
  everywhere else the app had been saying both.

- **Hermes Agent is a fourth AI client.** One button in Settings → AI clients,
  the same as the others, and it covers Hermes Desktop, the TUI and the CLI at
  once, because they share one config. Run `/reload-mcp` and the tools are
  there without leaving the conversation you are in.
- That config is YAML you are likely to have hand-written, so it is **edited
  rather than rewritten**: your comments, your ordering, your other servers and
  whatever you set on this one — a timeout, an environment variable, a tool
  filter — all stay exactly as they were. Only the command and the workspace
  are ours to write.
- Edits that arrive through Hermes are **marked as Hermes'**, beside the field,
  in the outline and in the page margin, like every other client's.

## 0.12.3

- **Release notes say what changed.** Every release used to carry the same
  paragraph about Apple quarantine and SmartScreen and nothing about the
  version you were installing. The notes now come from this file; the install
  note is still there, folded away at the bottom where it belongs.
- **An update comes to you.** When a new version is waiting the app shows it,
  with what changed in it, and installs and restarts in one step. It used to be
  a toast telling you to go and look in Settings, which is a notification about
  a notification. Saying **Later** means later — it will not ask again for that
  version, and Settings still has it whenever you want it.

## 0.12.2

- Claude can now record **which CV was sent for which application**, not only
  read it. Tailoring end to end — copy your base CV, edit it, attach it to the
  job — is one conversation instead of a conversation and then a click.
- A wrong path is refused rather than written: it has to be inside the
  workspace, exist, and be the right kind of document.
- `MCP.md` gained a **What it cannot do** section — a straight map of what an
  AI client can and cannot reach in your workspace, checked against the tools
  rather than remembered.

## 0.12.1

- The editor is **no longer a third tab**. You reach a document by opening it
  from an application or from your base CV, and **← Applications** — or `Esc` —
  takes you back to the application it was written for, with its row selected.
  If a filter was hiding that row, going back widens the filter to find it.
- The top bar **stops rearranging itself** when you switch screens.

## 0.12.0

- **The app opens on your applications.** The work is applying for jobs; a CV
  is something an application either has or has not got yet.
- **A base CV.** One document every tailored copy starts from, pinned above the
  list. It lives in the workspace rather than in the browser, so the AI clients
  you have connected can see it too.
- **Tailor in one click.** An application with no CV offers to make one: it
  copies the base, names it after the company and role, attaches it and opens
  it. What it was copied from is recorded, so every field you then change is
  marked as differing from the base.
- A row with a cover letter and no CV used to hide the letter. It doesn't now.

## 0.11.0

- **Add and remove sections and entries from the Form tab.** Every section has
  an Add under it, every entry a ×, and there is an Add a section at the end. A
  new entry copies the shape of its neighbours; a new section asks which kind
  once.
- Clearing a date no longer breaks the render. An empty date has to be absent,
  not empty, and it was being written as empty.

## 0.11.1

- **Clicking a block on the rendered page works again.** It had been silently
  broken on seven of the nine themes RenderCV ships: the page was a picture and
  nothing said why. Entries with no dates were the trigger.
- The page's click targets now hug the text column exactly, measured from the
  document rather than assumed from the image.

## 0.10.0

- **Form and YAML sit beside the page** instead of replacing it, so the render
  is on screen while you type rather than a tab away. Drag the divider to
  change the share; it stays where you put it.
- Put the caret in a field, or on a line of YAML, and the page highlights that
  entry and scrolls to it. Click a block on the page and the left pane goes to
  it.

## 0.9.0

- **Edit the CV on the page itself.** Click any block and a card opens beside
  it with that entry's fields; the page steps aside to make room. Arrow keys
  walk it from block to block. The form column that used to stand there
  permanently is gone.
- When the page cannot be made clickable, it now says so and why, instead of
  looking broken.

## 0.8.1

- The release build refuses to run when the tag and the app version disagree.
  v0.8.0 shipped binaries that called themselves 0.7.0, so the updater offered
  0.7.0 to machines already running it and nothing ever arrived.
