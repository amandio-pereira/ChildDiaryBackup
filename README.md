# ChildDiary backup

Script que faz backup de mensagens e fotos do [ChildDiary](https://app.childdiary.net) para HTML local, agrupado por mês.

## Requisitos

- Python 3.9+
- Windows (creation time nativo das fotos só funciona em Windows; noutros SOs só ajusta modified time)

## Instalação

```bash
pip install -r requirements.txt
playwright install chromium
```

Se der erro a compilar `greenlet` (falta "Microsoft Visual C++ 14.0 or greater"), força só wheels pré-compilados em vez de instalar o Build Tools todo:

```bash
pip install --only-binary=:all: -r requirements.txt
playwright install chromium
```

## Uso

```bash
python childdiary_backup.py
```

Na 1ª corrida abre um Chromium visível e pede email/password no terminal (não fica gravado em disco). Depois de autenticado, a sessão fica guardada em `pw_profile/` — corridas seguintes não pedem login outra vez.

O script percorre sempre todas as mensagens desde o início (backup completo, não incremental).

### Opções

| Flag | Default | Descrição |
|---|---|---|
| `--out` | `backup` | pasta de destino do backup |
| `--profile-dir` | `pw_profile` | pasta do perfil persistente do Chromium (sessão de login) |
| `--headless` | desligado | corre sem janela visível (só usar depois do 1º login já feito) |

Exemplo:

```bash
python childdiary_backup.py --out D:\backups\childdiary --headless
```

## Output

```
backup/
  index.html         # página de entrada: todas as mensagens (scroll infinito) + tab "Fotos" com todas as fotos
  2026-07/
    index.html       # mensagens do mês, texto + fotos + comentários
    photos/
      <entryId>_<mediaId>.jpg
  2026-08/
    ...
```

O `backup/index.html` tem: links para o index de cada mês, um feed combinado de todos os meses (mais recente primeiro) com scroll infinito (carrega em lotes conforme desces), e uma tab "Fotos" com todas as fotos de todos os meses juntas, também com scroll infinito. É tudo estático — funciona abrindo o ficheiro diretamente no browser, sem servidor.

Cada foto tem creation time e modified time ajustados para a data da mensagem/post correspondente.

## Executável standalone (sem Python no PC de destino)

Empacota Python + dependências + driver do Playwright numa pasta que corre em qualquer PC Windows sem instalar nada. O Chromium em si **não** vai embutido (isso pesava ~300MB) — o `.exe` descarrega-o sozinho na 1ª corrida no PC de destino.

No PC de desenvolvimento (este, que já tem Python/Playwright instalados):

```powershell
.\build_exe.ps1
```

Isto instala o `pyinstaller` e gera `dist\ChildDiaryBackup\`. Copia essa pasta **inteira** (não só o `.exe`) para o outro PC e corre `ChildDiaryBackup.exe` a partir de lá — nada de Python, pip ou `playwright install` manual no destino.

Notas:
- Pasta final fica bem mais leve (~100-150 MB, driver do Playwright em vez do Chromium completo).
- 1ª corrida no PC destino precisa de internet — descarrega o Chromium automaticamente (~150MB) antes de continuar. Corridas seguintes usam a cache local, sem voltar a descarregar.
- `pw_profile/` e `backup/` são criados ao lado do `.exe` na primeira corrida, tal como no script normal.
- Se voltares a mudar `childdiary_backup.py`, corre `build_exe.ps1` outra vez para atualizar o `.exe`.

## Notas

- Fotos são descarregadas via link assinado (SAS token) que expira em poucas horas — o script descarrega logo ao processar cada mensagem, não guarda os links para mais tarde.
- O schema de `Comments` da API nunca foi observado preenchido durante o desenho do script (amostras vazias). Se aparecerem comentários com aspeto estranho no HTML gerado, o autor/texto não bateu com os nomes de campo assumidos — é preciso ajustar `describe_comment()` em `childdiary_backup.py` com um exemplo real.
- Reexecutar o script não duplica fotos já descarregadas (verifica se o ficheiro já existe antes de pedir de novo à API).
