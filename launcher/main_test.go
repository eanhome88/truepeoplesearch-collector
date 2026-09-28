package main

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"
)

// This helper is a synthetic test process only: it never calls launcher main,
// Python, Docker, Git, a browser, a database, or a network service.
func TestHelperProcess(t *testing.T) {
	switch os.Getenv("TPS_LAUNCHER_TEST_CHILD") {
	case "block":
		time.Sleep(time.Minute)
		os.Exit(0)
	case "fail":
		os.Exit(7)
	}
}

func syntheticChild(t *testing.T, mode string) *managedCommand {
	t.Helper()
	cmd := exec.Command(os.Args[0], "-test.run=^TestHelperProcess$")
	cmd.Env = append(os.Environ(), "TPS_LAUNCHER_TEST_CHILD="+mode)
	child, err := startManagedCommand(cmd)
	if err != nil {
		t.Fatal(err)
	}
	return child
}

func TestChildStartDoesNotBlockReadiness(t *testing.T) {
	child := syntheticChild(t, "block")
	defer child.stop(time.Second)
	ready, err := waitForDashboard(context.Background(), child, func() bool { return true }, time.Second, time.Millisecond)
	if err != nil || !ready {
		t.Fatalf("readiness should run while the child is alive: %v %v", ready, err)
	}
	select {
	case <-child.done:
		t.Fatal("synthetic background child unexpectedly exited")
	default:
	}
}

func TestEarlyChildFailureDoesNotBecomeReady(t *testing.T) {
	child := syntheticChild(t, "fail")
	defer child.stop(time.Second)
	select {
	case <-child.done:
	case <-time.After(3 * time.Second):
		t.Fatal("synthetic child did not exit")
	}
	ready, err := waitForDashboard(context.Background(), child, func() bool { return true }, time.Second, time.Millisecond)
	if err == nil || ready || child.waitErr == nil {
		t.Fatalf("early child failure hidden: ready=%v err=%v wait=%v", ready, err, child.waitErr)
	}
}

func TestStopDoesNotTouchAnotherOwnedTree(t *testing.T) {
	first := syntheticChild(t, "block")
	second := syntheticChild(t, "block")
	defer second.stop(time.Second)
	if err := first.stop(time.Second); err != nil {
		t.Fatal(err)
	}
	select {
	case <-second.done:
		t.Fatal("stopping first launcher killed the unrelated child")
	default:
	}
}

func TestQuitCanCancelBeforeReadiness(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	child := &managedCommand{done: make(chan struct{})}
	cancel()
	ready, err := waitForDashboard(ctx, child, func() bool { return false }, time.Second, time.Millisecond)
	if ready || !errors.Is(err, context.Canceled) {
		t.Fatalf("cancel was ignored: %v %v", ready, err)
	}
}

func TestPortDeadlineDoesNotClaimReadiness(t *testing.T) {
	child := &managedCommand{done: make(chan struct{})}
	ready, err := waitForDashboard(context.Background(), child, func() bool { return false }, 5*time.Millisecond, time.Millisecond)
	if ready || err != nil {
		t.Fatalf("unavailable port must remain not ready: %v %v", ready, err)
	}
}

type fakeGuard struct {
	stops, forces, closes int
	onForce               func()
}

func (*fakeGuard) attach(*exec.Cmd) error     { return nil }
func (guard *fakeGuard) stop(*exec.Cmd) error { guard.stops++; return nil }
func (guard *fakeGuard) force(*exec.Cmd) error {
	guard.forces++
	if guard.onForce != nil {
		guard.onForce()
	}
	return nil
}
func (guard *fakeGuard) close() error { guard.closes++; return nil }

func TestStopDeadlineForcesAndWaitsForOwnedChild(t *testing.T) {
	guard := &fakeGuard{}
	child := &managedCommand{guard: guard, done: make(chan struct{})}
	guard.onForce = func() { close(child.done) }
	started := time.Now()
	if err := child.stop(5 * time.Millisecond); err != nil {
		t.Fatal(err)
	}
	if guard.stops != 1 || guard.forces != 1 || guard.closes != 1 || time.Since(started) > time.Second {
		t.Fatalf("shutdown was not bounded or complete: %+v", guard)
	}
}

func TestAlreadyExitedParentStillCleansOwnedDescendants(t *testing.T) {
	guard := &fakeGuard{}
	child := &managedCommand{guard: guard, done: make(chan struct{})}
	close(child.done)
	if err := child.stop(time.Second); err != nil {
		t.Fatal(err)
	}
	if guard.forces != 1 || guard.closes != 1 {
		t.Fatalf("exited parent skipped tree cleanup: %+v", guard)
	}
}

func TestCustomerReleaseEnvironmentDropsOutboundAndCollectionControls(t *testing.T) {
	env := customerReleaseEnvironment([]string{
		"TPS_DB_HOST=127.0.0.1",
		"TPS_ALERT_WEBHOOK=https://example.invalid/hook",
		"TPS_UPDATE_CHECK_URL=https://example.invalid/update",
		"PROXY_TUNNEL=socks5://example.invalid:1080",
		"TPS_ALLOW_CLUSTER=1",
		"TPS_RELEASE_MODE=standard",
	}, "token-1234567890123456")
	joined := "\n" + strings.Join(env, "\n") + "\n"
	for _, forbidden := range []string{
		"TPS_ALERT_WEBHOOK=", "TPS_UPDATE_CHECK_URL=", "PROXY_TUNNEL=",
		"TPS_ALLOW_CLUSTER=", "TPS_RELEASE_MODE=standard",
	} {
		if strings.Contains(joined, "\n"+forbidden) {
			t.Fatalf("customer environment retained %q: %s", forbidden, joined)
		}
	}
	if !strings.Contains(joined, "\nTPS_DB_HOST=127.0.0.1\n") ||
		!strings.Contains(joined, "\nTPS_RELEASE_MODE=customer\n") ||
		!strings.Contains(joined, "\nTPS_RELEASE_LAUNCH_TOKEN=token-1234567890123456\n") {
		t.Fatalf("customer environment lost required local settings: %s", joined)
	}
}

func TestCustomerLaunchTokenIsLongURLSafeHex(t *testing.T) {
	token, err := newCustomerLaunchToken()
	if err != nil {
		t.Fatal(err)
	}
	if len(token) != 48 {
		t.Fatalf("unexpected token length: %d", len(token))
	}
	for _, character := range token {
		if !strings.ContainsRune("0123456789abcdef", character) {
			t.Fatalf("token is not URL-safe hex: %q", token)
		}
	}
}
