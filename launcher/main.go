package main

import (
	"bufio"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"time"
)

func findPython(rootDir string) string {
	candidates := []string{
		filepath.Join(rootDir, ".venv", "Scripts", "python.exe"),
		filepath.Join(rootDir, ".venv", "bin", "python3"),
		filepath.Join(rootDir, "venv", "Scripts", "python.exe"),
	}
	for _, c := range candidates {
		if _, err := os.Stat(c); err == nil {
			return c
		}
	}
	if p, err := exec.LookPath("python.exe"); err == nil {
		return p
	}
	if p, err := exec.LookPath("python3"); err == nil {
		return p
	}
	if p, err := exec.LookPath("python"); err == nil {
		return p
	}
	return ""
}

// A customer launcher must not inherit controls that could turn a view-only
// local dashboard back into an outbound updater, alert sender, or proxy task.
// Database, Redis, and dashboard settings remain available through the
// approved local configuration path.
var customerEnvironmentExcluded = map[string]struct{}{
	"TPS_RELEASE_MODE":         {},
	"TPS_RELEASE_LAUNCH_TOKEN": {},
	"TPS_LOCAL_AUTH_REQUIRED":  {},
	"TPS_ALERT_WEBHOOK":        {},
	"TPS_UPDATE_CHECK_URL":     {},
	"PROXY_TUNNEL":             {},
	"PROXY_API":                {},
	"PROXY_FILE":               {},
	"TPS_ALLOW_CLUSTER":        {},
	"TPS_CONCURRENCY":          {},
	"TPS_START_AREA":           {},
}

func customerReleaseEnvironment(parent []string, launchToken string) []string {
	filtered := make([]string, 0, len(parent)+2)
	for _, entry := range parent {
		key, _, found := strings.Cut(entry, "=")
		if !found {
			continue
		}
		if _, excluded := customerEnvironmentExcluded[strings.ToUpper(key)]; excluded {
			continue
		}
		filtered = append(filtered, entry)
	}
	filtered = append(filtered, "TPS_RELEASE_MODE=customer")
	filtered = append(filtered, "TPS_LOCAL_AUTH_REQUIRED=1")
	if launchToken != "" {
		filtered = append(filtered, "TPS_RELEASE_LAUNCH_TOKEN="+launchToken)
	}
	return filtered
}

func newCustomerLaunchToken() (string, error) {
	bytes := make([]byte, 24)
	if _, err := rand.Read(bytes); err != nil {
		return "", fmt.Errorf("create local launch token: %w", err)
	}
	return hex.EncodeToString(bytes), nil
}

func customerDashboardURL(rootDir, pythonExe string) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	configScript := filepath.Join(rootDir, "scripts", "tps_env.py")
	cmd := exec.CommandContext(ctx, pythonExe, configScript, "--dashboard-port")
	cmd.Dir = rootDir
	cmd.Env = customerReleaseEnvironment(os.Environ(), "")
	output, err := cmd.Output()
	if err != nil {
		return "", errors.New("无法读取受控本机面板端口；请检查已批准的主机配置")
	}
	port, err := strconv.Atoi(strings.TrimSpace(string(output)))
	if err != nil || port < 1 || port > 65535 {
		return "", errors.New("受控本机面板端口无效")
	}
	return fmt.Sprintf("http://127.0.0.1:%d", port), nil
}

func customerDashboardIdentityMatches(dashboardURL, launchToken string) bool {
	if launchToken == "" {
		return false
	}
	ctx, cancel := context.WithTimeout(context.Background(), 800*time.Millisecond)
	defer cancel()
	client := &http.Client{Timeout: 800 * time.Millisecond}
	unauthenticated, err := http.NewRequestWithContext(ctx, http.MethodGet, dashboardURL+"/api/system/version", nil)
	if err != nil {
		return false
	}
	denied, err := client.Do(unauthenticated)
	if err != nil {
		return false
	}
	_ = denied.Body.Close()
	if denied.StatusCode != http.StatusUnauthorized {
		return false
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, dashboardURL+"/api/system/version", nil)
	if err != nil {
		return false
	}
	request.Header.Set("Authorization", "Bearer "+launchToken)
	response, err := client.Do(request)
	if err != nil {
		return false
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return false
	}
	var payload struct {
		OK          bool   `json:"ok"`
		ReleaseMode string `json:"release_mode"`
		LaunchToken string `json:"launch_token"`
	}
	if err := json.NewDecoder(io.LimitReader(response.Body, 64*1024)).Decode(&payload); err != nil {
		return false
	}
	return payload.OK && payload.ReleaseMode == "customer" && payload.LaunchToken == ""
}

func customerBrowserURL(dashboardURL, launchToken string) string {
	return dashboardURL + "/#access_token=" + url.QueryEscape(launchToken)
}

func startDocker(rootDir string) {
	composeFile := filepath.Join(rootDir, "deploy", "docker-compose.yml")
	if _, err := os.Stat(composeFile); err != nil {
		return
	}
	if _, err := exec.LookPath("docker"); err != nil {
		fmt.Println("  [1/4] 本地数据库状态: 使用原生 TiDB(4000) / Redis(6379)")
		return
	}
	fmt.Println("  [1/4] 正在启动本地数据库 (TiDB) 与消息队列 (Redis)...")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "docker", "compose", "-f", composeFile, "up", "-d")
	cmd.Dir = rootDir
	_ = cmd.Run()
}

func syncGitUpdate(rootDir string) {
	gitDir := filepath.Join(rootDir, ".git")
	if _, err := os.Stat(gitDir); err != nil {
		return
	}
	if _, err := exec.LookPath("git"); err != nil {
		return
	}
	fmt.Println("  [2/4] 正在检查云端版本更新...")
	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "git", "pull", "--ff-only", "origin", "main")
	cmd.Dir = rootDir
	out, err := cmd.CombinedOutput()
	if err == nil {
		outStr := strings.TrimSpace(string(out))
		if !strings.Contains(outStr, "Already up to date") && !strings.Contains(outStr, "已经是最新") {
			fmt.Println("     ✅ 成功同步最新云端更新！")
		}
	}
}

func openBrowserWithRunner(platform, targetURL string, run func(string, ...string) error) error {
	name := "xdg-open"
	args := []string{targetURL}
	if platform == "windows" {
		name = "cmd"
		args = []string{"/c", "start", "", targetURL}
	} else if platform == "darwin" {
		name = "open"
	}
	if err := run(name, args...); err != nil {
		// The launch URL contains a bearer capability. Never wrap a command error
		// because its text could include that URL and leak the token to logs.
		return errors.New("默认浏览器未能打开")
	}
	return nil
}

func openBrowser(targetURL string) error {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	return openBrowserWithRunner(runtime.GOOS, targetURL, func(name string, args ...string) error {
		return exec.CommandContext(ctx, name, args...).Run()
	})
}

// A platform guard owns only this launcher's child process tree. In particular,
// a failed duplicate launch must never run the installation-wide stop command.
type processGuard interface {
	attach(*exec.Cmd) error
	stop(*exec.Cmd) error
	force(*exec.Cmd) error
	close() error
}

type managedCommand struct {
	cmd     *exec.Cmd
	guard   processGuard
	done    chan struct{}
	waitErr error // read only after done closes
}

func startManagedCommand(cmd *exec.Cmd) (*managedCommand, error) {
	guard, err := newProcessGuard(cmd)
	if err != nil {
		return nil, err
	}
	if err := cmd.Start(); err != nil {
		_ = guard.close()
		return nil, err
	}
	if err := guard.attach(cmd); err != nil {
		// Windows starts suspended until ownership is established. Unix starts
		// in its own group, so failure cleanup cannot target another instance.
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
		_ = guard.close()
		return nil, err
	}
	child := &managedCommand{cmd: cmd, guard: guard, done: make(chan struct{})}
	go func() {
		child.waitErr = cmd.Wait()
		close(child.done)
	}()
	return child, nil
}

func (child *managedCommand) stop(timeout time.Duration) error {
	defer child.guard.close()
	stopErr := child.guard.stop(child.cmd)
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case <-child.done:
		// The parent can exit before its descendants; force only the owned tree.
		return child.guard.force(child.cmd)
	case <-timer.C:
		if err := child.guard.force(child.cmd); err != nil {
			return fmt.Errorf("owned process cleanup failed: %w", err)
		}
		select {
		case <-child.done:
			return stopErr
		case <-time.After(2 * time.Second):
			return errors.New("owned process did not exit after forced cleanup")
		}
	}
}

func waitForDashboard(ctx context.Context, child *managedCommand, probe func() bool, timeout, interval time.Duration) (bool, error) {
	deadline := time.NewTimer(timeout)
	defer deadline.Stop()
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return false, ctx.Err()
		case <-child.done:
			return false, fmt.Errorf("supervisor exited before readiness: %v", child.waitErr)
		default:
		}
		if probe() {
			select {
			case <-child.done:
				return false, fmt.Errorf("supervisor exited before readiness: %v", child.waitErr)
			case <-ctx.Done():
				return false, ctx.Err()
			default:
				return true, nil
			}
		}
		select {
		case <-ctx.Done():
			return false, ctx.Err()
		case <-child.done:
			return false, fmt.Errorf("supervisor exited before readiness: %v", child.waitErr)
		case <-deadline.C:
			return false, nil
		case <-ticker.C:
		}
	}
}

func main() {
	execPath, err := os.Executable()
	if err != nil {
		execPath, _ = filepath.Abs(".")
	}
	rootDir := filepath.Dir(execPath)
	_ = os.Chdir(rootDir)

	fmt.Println("============================================================")
	fmt.Println("  TruePeopleSearch 企业情报中心 · 桌面启动客户端")
	fmt.Println("============================================================")
	fmt.Printf("  • 运行目录: %s\n", rootDir)

	pythonExe := findPython(rootDir)
	if pythonExe == "" {
		fmt.Println("\n❌ 错误: 未找到 Python 运行环境！")
		fmt.Println("   请完成已批准的主机准备流程并安装 Python 3.9+。")
		fmt.Println("   客户离线包不运行旧的在线安装器。")
		fmt.Println("\n按回车键退出...")
		_, _ = bufio.NewReader(os.Stdin).ReadString('\n')
		return
	}
	fmt.Printf("  • Python环境: %s\n", pythonExe)
	dashboardURL, err := customerDashboardURL(rootDir, pythonExe)
	if err != nil {
		fmt.Printf("\n❌ 错误: %v\n", err)
		return
	}
	launchToken, err := newCustomerLaunchToken()
	if err != nil {
		fmt.Printf("\n❌ 错误: 无法创建本次本机启动标识: %v\n", err)
		return
	}
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	go func() {
		reader := bufio.NewReader(os.Stdin)
		for {
			text, err := reader.ReadString('\n')
			if strings.TrimSpace(strings.ToLower(text)) == "q" {
				cancel()
				return
			}
			if err != nil {
				return
			}
		}
	}()

	// 客户离线发布包不应隐式修改本机环境：不自动启动 Docker，
	// 不拉取 Git。数据库和 Redis 必须由安装/运维流程显式准备。
	fmt.Println("  [1/4] 客户端包不自动启动 Docker 或同步 Git 代码。")
	if ctx.Err() != nil {
		return
	}

	// 仅启动回环地址上的 Dashboard。任何后台作业都必须由经过授权的
	// 运维流程显式发起，不能因为客户验收启动器而自动运行。
	fmt.Println("  [2/4] 正在启动本机控制台（不启动后台作业）...")
	supScript := filepath.Join(rootDir, "scripts", "tps_supervisor.py")
	supCmd := exec.Command(pythonExe, supScript, "start", "--dashboard-only")
	supCmd.Dir = rootDir
	// The supported customer launcher always starts the dashboard in its local
	// view-only release mode with a fresh per-launch identity. It does not
	// inherit outbound update, alert, proxy, or collection controls.
	supCmd.Env = customerReleaseEnvironment(os.Environ(), launchToken)
	// GUI-subsystem builds may have no valid inherited console handles.
	if _, err := os.Stdout.Stat(); err == nil {
		supCmd.Stdout = os.Stdout
	}
	if _, err := os.Stderr.Stat(); err == nil {
		supCmd.Stderr = os.Stderr
	}
	child, err := startManagedCommand(supCmd)
	if err != nil {
		fmt.Printf("  ❌ 守护进程未启动: %v\n", err)
		return
	}
	defer func() {
		fmt.Println("\n正在关闭本次启动的后台服务...")
		if err := child.stop(35 * time.Second); err != nil {
			fmt.Printf("⚠️ 后台清理未确认完成: %v\n", err)
		} else {
			fmt.Println("✅ 本次启动的后台服务已停止。")
		}
	}()

	// 3. Only accept the dashboard instance started by this launcher. A port
	// listener alone could be an older or unrelated local process.
	ready, err := waitForDashboard(ctx, child, func() bool {
		return customerDashboardIdentityMatches(dashboardURL, launchToken)
	}, 9*time.Second, 300*time.Millisecond)
	if err != nil {
		if !errors.Is(err, context.Canceled) {
			fmt.Printf("  ❌ 后台服务启动失败: %v\n", err)
		}
		return
	}

	if !ready {
		fmt.Printf("  ❌ 控制台未就绪；本机地址: %s\n", dashboardURL)
		fmt.Println("  请检查本地服务日志与端口配置后重新运行启动器；本次进程会关闭。")
		return
	}
	fmt.Printf("  [3/4] 控制台端口已响应，正在打开: %s\n", dashboardURL)
	if err := openBrowser(customerBrowserURL(dashboardURL, launchToken)); err != nil {
		fmt.Printf("  ❌ %s；本机地址: %s\n", err, dashboardURL)
		fmt.Println("  此地址不含授权令牌，不能直接作为登录链接。请修复默认浏览器后重新运行启动器；本次进程会关闭。")
		return
	}

	fmt.Println("============================================================")
	fmt.Println("  本机控制台已打开；此状态不代表数据库、队列或后台作业已验证。")
	fmt.Printf("  控制台地址: %s\n", dashboardURL)
	fmt.Println("------------------------------------------------------------")
	fmt.Println("  提示: 输入 q 并回车 或按 Ctrl+C 可停止本次启动的进程树。")
	fmt.Println("  后台作业不会随本启动器自动运行。")
	if runtime.GOOS == "windows" {
		fmt.Println("  Windows 退出会强制终止本次启动的进程树，不保证未完成写入已落库。")
	}
	fmt.Println("============================================================")

	select {
	case <-ctx.Done():
	case <-child.done:
		fmt.Printf("\n⚠️ 后台守护进程已退出: %v\n", child.waitErr)
	}
}
