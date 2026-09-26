package main

import (
	"bufio"
	"context"
	"fmt"
	"net"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
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

func openBrowser(url string) {
	var cmd *exec.Cmd
	if runtime.GOOS == "windows" {
		cmd = exec.Command("cmd", "/c", "start", url)
	} else if runtime.GOOS == "darwin" {
		cmd = exec.Command("open", url)
	} else {
		cmd = exec.Command("xdg-open", url)
	}
	_ = cmd.Start()
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
		fmt.Println("   请确认已安装 Python 3.9+，或先运行 deploy\\install.bat 完成初始化。")
		fmt.Println("\n按回车键退出...")
		_, _ = bufio.NewReader(os.Stdin).ReadString('\n')
		return
	}
	fmt.Printf("  • Python环境: %s\n", pythonExe)

	// 1. 启动 Docker 容器 (TiDB + Redis)
	startDocker(rootDir)

	// 2. 检查云端更新
	syncGitUpdate(rootDir)

	// 3. 启动后台 Supervisor
	fmt.Println("  [3/4] 正在启动后台守护集群与 Web 控制台...")
	supScript := filepath.Join(rootDir, "scripts", "tps_supervisor.py")
	supCmd := exec.Command(pythonExe, supScript, "start")
	supCmd.Dir = rootDir
	if err := supCmd.Run(); err != nil {
		fmt.Printf("  ⚠️ 守护进程启动提示: %v\n", err)
	}

	// 4. 等待面板端口 5001 响应
	dashboardURL := "http://127.0.0.1:5001"
	ready := false
	for i := 0; i < 15; i++ {
		conn, err := net.DialTimeout("tcp", "127.0.0.1:5001", 300*time.Millisecond)
		if err == nil {
			_ = conn.Close()
			ready = true
			break
		}
		time.Sleep(300 * time.Millisecond)
	}

	fmt.Printf("  [4/4] 服务已就绪，正在打开控制台界面: %s\n", dashboardURL)
	if ready {
		openBrowser(dashboardURL)
	} else {
		fmt.Printf("  ℹ️ 请在浏览器中手动打开: %s\n", dashboardURL)
	}

	fmt.Println("============================================================")
	fmt.Println("  ✅ 系统正在后台稳定运行！")
	fmt.Printf("  控制台地址: %s\n", dashboardURL)
	fmt.Println("  代理配置页: http://127.0.0.1:5001/#/proxy")
	fmt.Println("------------------------------------------------------------")
	fmt.Println("  提示: 输入 q 并回车 或按 Ctrl+C 可平稳关闭所有后台服务。")
	fmt.Println("============================================================")

	// 监听退出信号
	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, os.Interrupt, syscall.SIGTERM)

	go func() {
		reader := bufio.NewReader(os.Stdin)
		for {
			text, err := reader.ReadString('\n')
			if err != nil {
				return
			}
			if strings.TrimSpace(strings.ToLower(text)) == "q" {
				sigChan <- os.Interrupt
				return
			}
		}
	}()

	<-sigChan
	fmt.Println("\n正在平稳关闭后台爬虫服务与数据库连接...")
	stopCmd := exec.Command(pythonExe, supScript, "stop")
	stopCmd.Dir = rootDir
	_ = stopCmd.Run()
	fmt.Println("✅ 所有后台服务已平稳关闭。感谢使用！")
	time.Sleep(1 * time.Second)
}
