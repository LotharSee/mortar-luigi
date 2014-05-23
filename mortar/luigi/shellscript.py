import luigi
import abc
import subprocess 
import logging
from mortar.luigi import target_factory

logger = logging.getLogger('luigi-interface')
class ShellScriptTask(luigi.Task):
    token_path = luigi.Parameter()

    def output_token(self):
        """
        Token written out to indicate finished shell script
        """
        return target_factory.get_target('%s/%s' % (self.token_path, self.__class__.__name__))

    def output(self):
        return [self.output_token()]

    @abc.abstractmethod
    def subprocess_commands(self):
        """
        Shell commands that will be run in a subprocess
        Should return a string where each line of script is separated with ';'
        Example:
            cd my/dir; ls;
        """
        raise RuntimeError("Must implement subprocess_commands!")
    
    def run(self):
        cmd = self.subprocess_commands()
        output = subprocess.Popen(
            cmd,
            shell=True,
            stdout = subprocess.PIPE,
            stderr = subprocess.PIPE
        )
        out, err = output.communicate()
        rc = output.returncode
        # generate output message
        message = self._create_message(cmd, out, err, rc)
        self._check_error(rc, err, message)

        self.cmd_output = {
          'cmd'         : cmd,
          'stdout'      : out,
          'stderr'      : err,
          'return_code' : rc
        }
        logger.debug('%s - output:%s' % (self.__class__.__name__, message))
        if err == '':
            target_factory.write_file(self.output_token())

    def _create_message(self, cmd, out, err, rc):
        message = ''
        message += '\n-----------------------------'
        message += '\nCMD         : %s' % cmd
        message += '\nSTDOUT      : %s' % repr(out)
        message += '\nSTDERR      : %s' % repr(err)
        message += '\nRETURN CODE : %d' % rc
        message += '\n-----------------------------'
        return message

    def _check_error(self, rc, err, message):
        if err != '' or rc != 0:
            raise RuntimeError(message)
            
