# Hotmart Course Downloader

Ferramenta para criar um backup **local** de aulas e materiais de cursos aos quais a
própria conta autenticada possui acesso legítimo. O programa organiza módulos e aulas,
baixa arquivos disponibilizados ao aluno, preserva links e descrições e pode continuar
uma execução interrompida.

## Origem e créditos

Este repositório é um fork de
[`magosheimus/hotmart-course-downloader`](https://github.com/magosheimus/hotmart-course-downloader),
que por sua vez foi baseado no
[gist de @juvenal](https://gist.github.com/juvenal/2d9a822325769d30c45c635fbf388c1b).
Os créditos e o histórico do projeto original foram preservados.

## Segurança

- O email é lido no terminal e a senha é solicitada com `getpass`, sem aparecer na tela.
- Credenciais e tokens existem somente em memória durante a execução.
- O programa não lê senhas, cookies nem sessões do navegador.
- Logs são filtrados e contêm somente eventos operacionais.
- Respostas completas da API e URLs temporárias não são gravadas em arquivos de debug.
- Downloads externos usam uma sessão HTTP limpa, sem o cabeçalho de autenticação da Hotmart.
- `Cursos/`, logs, arquivos temporários, ambientes virtuais e configurações locais estão no `.gitignore`.
- Fluxos de vídeo criptografados ou com DRM são registrados e ignorados; a ferramenta não
  tenta remover nem contornar proteção.

Nunca coloque email, senha, cookies ou tokens no código, em `.env`, em arquivos de
configuração ou em commits.

## Requisitos

- Windows 10 ou 11
- Python 3.10 ou mais recente
- Git
- FFmpeg e FFprobe disponíveis no `PATH`
- Espaço livre suficiente para os cursos

## Instalação no Windows

No PowerShell, dentro da pasta do projeto:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
ffmpeg -version
```

Se o FFmpeg não estiver instalado, use uma distribuição confiável e confirme que os
comandos `ffmpeg` e `ffprobe` funcionam antes de iniciar os downloads.

## Configuração do curso

A listagem automática da API pode retornar vazia. Nesse caso, copie o modelo abaixo para
um arquivo chamado `config_local.py`:

```python
CURSOS_SUBDOMINIOS = ["nome-do-subdominio"]
```

O arquivo `config_local.py` é local e ignorado pelo Git. O subdomínio é o trecho entre
`/club/` e `/products/` na URL visível da área do aluno:

```text
https://hotmart.com/pt-BR/club/nome-do-subdominio/products/...
```

Também é possível informar um subdomínio somente para uma execução:

```powershell
.\.venv\Scripts\python.exe hotmark.py --subdomain nome-do-subdominio
```

## Execução

Execução normal:

```powershell
.\.venv\Scripts\python.exe hotmark.py
```

O terminal solicitará:

```text
Email da Hotmart:
Senha da Hotmart:
```

A senha não é exibida. Para o teste mínimo antes do download completo:

```powershell
.\.venv\Scripts\python.exe hotmark.py --curso 1 --limite-aulas 1
```

Depois de conferir o resultado, rode novamente sem `--limite-aulas` para processar o curso
inteiro.

### Ponte opcional para uma sessão já aberta no Chrome

Para contas com login social, a pasta `browser_bridge/` contém uma extensão local mínima.
Ela observa somente requisições `.m3u8` da Hotmart e entrega o endereço temporário a um
servidor em `127.0.0.1`. A URL fica apenas em memória: não aparece no terminal, não entra
na linha de comando do FFmpeg e não é gravada em arquivo. A extensão não solicita acesso
a cookies, downloads, histórico ou a outros sites.

Carregue a pasta `browser_bridge/` como extensão descompactada e, antes de reproduzir uma
aula, inicie a ponte indicando o arquivo de destino:

```powershell
.\.venv\Scripts\python.exe browser_bridge.py --output "Cursos\Curso\01 - Módulo\01 - Aula\aula.mp4"
```

Se o manifesto tiver `EXT-X-KEY`, `EXT-X-SESSION-KEY` ou outra forma de criptografia, a
ponte registra apenas que o vídeo está protegido e encerra sem baixar segmentos. Ela não
implementa descriptografia nem contorno de DRM.

Um índice coletado legitimamente pela interface do curso pode ser transformado na
estrutura completa de pastas com:

```powershell
.\.venv\Scripts\python.exe organize_browser_course.py "Cursos\Curso\indice.json"
```

### Limitações de autenticação e vídeo

O fluxo principal implementa somente email e senha e não importa cookies, tokens ou
sessões do navegador. Contas que aceitam apenas login social (Google/Apple) ou código
temporário não conseguem autenticar diretamente na API. A ponte opcional descrita acima
usa a sessão já aberta apenas para observar uma requisição de mídia que o próprio player
fez; ela não transforma a sessão social em credenciais da API.

Além disso, a Hotmart informa que vídeos hospedados no Hotmart Player só possuem download
oficial para o produtor ou para usuários com papel de editor. Quando o aluno não recebe um
botão normal de download, este projeto não tenta extrair o token interno do player nem
contornar essa restrição. Consulte a
[documentação oficial do Hotmart Player](https://help.hotmart.com/pt-br/article/360038450972/como-funciona-a-biblioteca-de-videos-do-hotmart-player-).

## Estrutura dos arquivos

```text
Cursos/
└── Nome do Curso/
    ├── 01 - Nome do Módulo/
    │   ├── 01 - Nome da Aula/
    │   │   ├── aula.mp4
    │   │   ├── descricao.md
    │   │   ├── links.md
    │   │   └── Materiais/
    │   │       ├── material.pdf
    │   │       └── planilha.xlsx
    │   └── 02 - Nome da Aula/
    └── 02 - Nome do Módulo/
```

Módulos e aulas são numerados na ordem fornecida pela Hotmart. Acentos são preservados;
somente caracteres incompatíveis com nomes de arquivos do Windows são removidos. Quando
uma aula possui vários vídeos, os nomes são `01 - video.mp4`, `02 - video.mp4` etc.

## Retomada e tratamento de erros

Arquivos são gravados primeiro com a extensão `.part` e somente passam ao nome definitivo
após a conclusão. Arquivos existentes e válidos são mantidos. Vídeos são verificados com
FFprobe quando disponível. Um erro em uma aula ou material é registrado sem interromper as
demais aulas.

Ao final, o programa mostra quantidades de aulas, vídeos, materiais, links, falhas e vídeos
protegidos/não suportados, além do caminho de `download.log`.

## Antes de enviar alterações ao GitHub

Confira:

```powershell
git status --short --ignored
git check-ignore Cursos download.log config_local.py
```

Não faça `git add -f` em conteúdo ignorado. `Cursos/`, vídeos, PDFs, logs, tokens,
credenciais e configuração local nunca devem ser enviados ao GitHub.

## Uso responsável

Use esta ferramenta somente para conteúdo ao qual sua própria conta possua acesso
legítimo e respeite os termos aplicáveis. Não use o projeto para compartilhar conteúdo,
contornar controle de acesso ou quebrar DRM.
