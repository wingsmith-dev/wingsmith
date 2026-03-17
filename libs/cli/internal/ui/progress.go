package ui

import (
	"fmt"
	"io"
	"strings"
	"sync"
	"time"
)

// Reporter narrates long-running work. Every write goes to stderr so that
// stdout stays pure data even while a spinner is running.
type Reporter interface {
	// Step announces work starting; the returned func closes it out.
	Step(format string, args ...any) StepHandle
	// Info prints an informational line.
	Info(format string, args ...any)
	// Success prints a completed line without an associated step.
	Success(format string, args ...any)
	// Warn prints a warning line.
	Warn(format string, args ...any)
	// Hint prints a suggested next command.
	Hint(description, command string)
	// Progress renders a determinate bar; returns a setter and a finisher.
	Progress(label string, total int64) ProgressHandle
	// Stop tears down any live animation.
	Stop()
}

// StepHandle completes a step announced by Reporter.Step.
type StepHandle interface {
	Done(format string, args ...any)
	Fail(format string, args ...any)
	Update(format string, args ...any)
}

// ProgressHandle drives a determinate progress bar.
type ProgressHandle interface {
	Set(current int64)
	Done(format string, args ...any)
}

// NewReporter picks the right narration strategy for the session.
//   - animate: TTY, live spinner and in-place bars
//   - plain:   non-TTY human output, one line per transition
//   - silent:  quiet, agent, or machine output modes
func NewReporter(w io.Writer, style Style, animate, silent bool) Reporter {
	if silent {
		return silentReporter{}
	}
	return &ttyReporter{w: w, style: style, animate: animate, spinAt: 150 * time.Millisecond}
}

type silentReporter struct{}

func (silentReporter) Step(string, ...any) StepHandle        { return silentStep{} }
func (silentReporter) Info(string, ...any)                   {}
func (silentReporter) Success(string, ...any)                {}
func (silentReporter) Warn(string, ...any)                   {}
func (silentReporter) Hint(string, string)                   {}
func (silentReporter) Progress(string, int64) ProgressHandle { return silentProgress{} }
func (silentReporter) Stop()                                 {}

type silentStep struct{}

func (silentStep) Done(string, ...any)   {}
func (silentStep) Fail(string, ...any)   {}
func (silentStep) Update(string, ...any) {}

type silentProgress struct{}

func (silentProgress) Set(int64)           {}
func (silentProgress) Done(string, ...any) {}

type ttyReporter struct {
	mu      sync.Mutex
	w       io.Writer
	style   Style
	animate bool
	spinAt  time.Duration
	active  *ttyStep
}

var frames = []string{"⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"}

func (r *ttyReporter) clear() {
	if r.animate {
		fmt.Fprint(r.w, "\r\x1b[2K")
	}
}

func (r *ttyReporter) Step(format string, args ...any) StepHandle {
	r.Stop()
	s := &ttyStep{r: r, label: fmt.Sprintf(format, args...), start: time.Now(), stop: make(chan struct{})}
	r.mu.Lock()
	r.active = s
	r.mu.Unlock()
	if r.animate {
		go s.spin()
	} else {
		fmt.Fprintf(r.w, "%s %s\n", r.style.Arrow(), r.style.Dim(s.label))
	}
	return s
}

func (r *ttyReporter) Info(format string, args ...any) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.clear()
	fmt.Fprintf(r.w, "%s %s\n", r.style.Bullet(), fmt.Sprintf(format, args...))
}

func (r *ttyReporter) Success(format string, args ...any) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.clear()
	fmt.Fprintf(r.w, "%s %s\n", r.style.Tick(), fmt.Sprintf(format, args...))
}

func (r *ttyReporter) Warn(format string, args ...any) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.clear()
	fmt.Fprintf(r.w, "%s %s\n", r.style.Bang(), r.style.Amber(fmt.Sprintf(format, args...)))
}

func (r *ttyReporter) Hint(description, command string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.clear()
	if command == "" {
		fmt.Fprintf(r.w, "%s %s\n", r.style.Hook(), r.style.Dim(description))
		return
	}
	fmt.Fprintf(r.w, "%s %s  %s\n", r.style.Hook(), r.style.Cyan(command), r.style.Dim(description))
}

func (r *ttyReporter) Stop() {
	r.mu.Lock()
	s := r.active
	r.active = nil
	r.mu.Unlock()
	if s != nil {
		s.halt()
	}
}

func (r *ttyReporter) Progress(label string, total int64) ProgressHandle {
	r.Stop()
	return &ttyProgress{r: r, label: label, total: total, start: time.Now()}
}

type ttyStep struct {
	r       *ttyReporter
	label   string
	start   time.Time
	stop    chan struct{}
	once    sync.Once
	mu      sync.Mutex
	stopped bool
}

func (s *ttyStep) spin() {
	t := time.NewTicker(90 * time.Millisecond)
	defer t.Stop()
	shown := false
	for i := 0; ; i++ {
		select {
		case <-s.stop:
			return
		case <-t.C:
			if !shown && time.Since(s.start) < s.r.spinAt {
				continue
			}
			shown = true
			s.mu.Lock()
			label := s.label
			s.mu.Unlock()
			s.r.mu.Lock()
			if s.r.active == s {
				fmt.Fprintf(s.r.w, "\r\x1b[2K%s %s", s.r.style.Cyan(frames[i%len(frames)]), s.r.style.Dim(label))
			}
			s.r.mu.Unlock()
		}
	}
}

func (s *ttyStep) halt() {
	s.once.Do(func() {
		close(s.stop)
		s.mu.Lock()
		s.stopped = true
		s.mu.Unlock()
		s.r.mu.Lock()
		s.r.clear()
		s.r.mu.Unlock()
	})
}

func (s *ttyStep) Update(format string, args ...any) {
	s.mu.Lock()
	s.label = fmt.Sprintf(format, args...)
	s.mu.Unlock()
}

func (s *ttyStep) finish(sym, text string) {
	s.halt()
	s.r.mu.Lock()
	defer s.r.mu.Unlock()
	if s.r.active == s {
		s.r.active = nil
	}
	// A step that finished before the clock could resolve it has no timing
	// worth reporting; "0ms" is noise, not information.
	if elapsed := time.Since(s.start); elapsed >= time.Millisecond {
		fmt.Fprintf(s.r.w, "%s %s %s\n", sym, text, s.r.style.Dim(FormatDuration(elapsed)))
		return
	}
	fmt.Fprintf(s.r.w, "%s %s\n", sym, text)
}

func (s *ttyStep) Done(format string, args ...any) {
	s.finish(s.r.style.Tick(), fmt.Sprintf(format, args...))
}

func (s *ttyStep) Fail(format string, args ...any) {
	s.finish(s.r.style.Cross(), s.r.style.Red(fmt.Sprintf(format, args...)))
}

type ttyProgress struct {
	r       *ttyReporter
	label   string
	total   int64
	current int64
	start   time.Time
	last    time.Time
}

func (p *ttyProgress) Set(current int64) {
	p.current = current
	if !p.r.animate {
		return
	}
	if time.Since(p.last) < 60*time.Millisecond && current < p.total {
		return
	}
	p.last = time.Now()
	frac := 0.0
	if p.total > 0 {
		frac = float64(current) / float64(p.total)
	}
	p.r.mu.Lock()
	defer p.r.mu.Unlock()
	fmt.Fprintf(p.r.w, "\r\x1b[2K  %s %s %s",
		Bar(p.r.style, frac, 24),
		p.r.style.Text(fmt.Sprintf("%3.0f%%", frac*100)),
		p.r.style.Dim(p.label))
}

func (p *ttyProgress) Done(format string, args ...any) {
	p.r.mu.Lock()
	defer p.r.mu.Unlock()
	p.r.clear()
	fmt.Fprintf(p.r.w, "%s %s %s\n", p.r.style.Tick(), fmt.Sprintf(format, args...),
		p.r.style.Dim(FormatDuration(time.Since(p.start))))
}

// FormatDuration renders a compact, human duration ("38s", "2m14s", "3h").
func FormatDuration(d time.Duration) string {
	switch {
	case d < time.Second:
		return fmt.Sprintf("%dms", d.Milliseconds())
	case d < time.Minute:
		return fmt.Sprintf("%.0fs", d.Seconds())
	case d < time.Hour:
		return fmt.Sprintf("%dm%02ds", int(d.Minutes()), int(d.Seconds())%60)
	case d < 48*time.Hour:
		return fmt.Sprintf("%dh%02dm", int(d.Hours()), int(d.Minutes())%60)
	default:
		return fmt.Sprintf("%dd%02dh", int(d.Hours())/24, int(d.Hours())%24)
	}
}

// Ago renders a relative timestamp ("4m ago", "just now").
func Ago(t time.Time) string {
	if t.IsZero() {
		return ""
	}
	d := time.Since(t)
	if d < 5*time.Second {
		return "just now"
	}
	if d < 0 {
		return "in " + FormatDuration(-d)
	}
	return FormatDuration(d) + " ago"
}

// Indent prefixes every line of s with n spaces.
func Indent(s string, n int) string {
	pad := strings.Repeat(" ", n)
	lines := strings.Split(strings.TrimRight(s, "\n"), "\n")
	for i := range lines {
		lines[i] = pad + lines[i]
	}
	return strings.Join(lines, "\n")
}
