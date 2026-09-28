# claudex 的 bash 补全：source 本输出，或写进 bash-completion 目录
_claudex() {
  local cur prev
  cur=${COMP_WORDS[COMP_CWORD]}
  prev=${COMP_WORDS[COMP_CWORD-1]:-}
  local commands="init key probe login profiles status preflight update upgrade gateway completion"
  local options="--profile --fable --opus --sonnet --haiku --with-mcp --fast"
  case "$prev" in
    --fable|--opus|--sonnet|--haiku)
      mapfile -t COMPREPLY < <(compgen -W "$(claudex _complete refs 2>/dev/null)" -- "$cur")
      return 0
      ;;
    --profile)
      mapfile -t COMPREPLY < <(compgen -W "$(claudex _complete profiles 2>/dev/null)" -- "$cur")
      return 0
      ;;
    key)
      mapfile -t COMPREPLY < <(compgen -W "set" -- "$cur")
      return 0
      ;;
    set)
      mapfile -t COMPREPLY < <(compgen -W "$(claudex _complete generic-sources 2>/dev/null)" -- "$cur")
      return 0
      ;;
    probe)
      mapfile -t COMPREPLY < <(compgen -W "$(claudex _complete sources 2>/dev/null)" -- "$cur")
      return 0
      ;;
    login)
      mapfile -t COMPREPLY < <(compgen -W "codex antigravity" -- "$cur")
      return 0
      ;;
    gateway)
      mapfile -t COMPREPLY < <(compgen -W "start stop restart" -- "$cur")
      return 0
      ;;
    completion)
      mapfile -t COMPREPLY < <(compgen -W "bash" -- "$cur")
      return 0
      ;;
  esac
  # bash 默认的 COMP_WORDBREAKS 含 @，`claudex @da` 在 COMP_WORDS 里拆成 `@` 与 `da`，readline 替换的
  # 却是整个 `@da`，所以候选同样要带 @ 前缀
  if ((COMP_CWORD == 2)) && [[ $prev == @ ]]; then
    mapfile -t COMPREPLY < <(compgen -P @ -W "$(claudex _complete profiles 2>/dev/null)" -- "$cur")
    return 0
  fi
  if [[ $cur == @* ]]; then
    local names
    names=$(claudex _complete profiles 2>/dev/null)
    mapfile -t COMPREPLY < <(compgen -P @ -W "$names" -- "${cur#@}")
    return 0
  fi
  if ((COMP_CWORD == 1)); then
    mapfile -t COMPREPLY < <(compgen -W "$commands $options" -- "$cur")
    return 0
  fi
  mapfile -t COMPREPLY < <(compgen -W "$options" -- "$cur")
}
complete -F _claudex claudex
