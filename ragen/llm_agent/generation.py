import torch
import re
from collections import defaultdict
import os
from typing import List, Dict, Any, Tuple
from dataclasses import dataclass
from .tensor_helper import TensorHelper, TensorConfig
from ragen.utils import set_seed
from ragen.utils.plot import (
    save_trajectory_to_output,
    parse_llm_output
)
from verl import DataProto
import shutil

@dataclass
class GenerationConfig:
    max_turns: int
    max_start_length: int
    max_prompt_length: int 
    max_response_length: int
    max_obs_length: int
    logging: dict

class LLMGenerationManager:
    def __init__(
        self,
        tokenizer,
        actor_rollout_wg,
        env_class,
        config: GenerationConfig
    ):
        self.tokenizer = tokenizer
        self.actor_rollout_wg = actor_rollout_wg
        self.env_class = env_class
        self.config = config
        
        self.tensor_fn = TensorHelper(TensorConfig(
            pad_token_id=tokenizer.pad_token_id,
            max_prompt_length=config.max_prompt_length,
            max_obs_length=config.max_obs_length,
            max_start_length=config.max_start_length
        ))

    def _batch_tokenize(self, responses: List[str]) -> torch.Tensor:
        """Tokenize a batch of responses."""
        return self.tokenizer(
            responses, 
            add_special_tokens=False, 
            return_tensors='pt', 
            padding="longest"
        )['input_ids']

    def _postprocess_responses(self, responses: torch.Tensor) -> torch.Tensor:
        """Process responses to remove 1. multiple answers or 2. reward hacking attempts."""
        # Remove everything after </answer> but keep the tag
        responses_str = self.tokenizer.batch_decode(
            responses, 
            skip_special_tokens=True
        )

        responses_str = [resp.split('</answer>')[0] + '</answer>' 
                    if '</answer>' in resp else resp 
                    for resp in responses_str]
        # Remove reward hacking patterns
        hack_pattern = r'reward: \d+\.\d+\n|done: (True|False)\n'
        hacked = [resp for resp in responses_str if re.search(hack_pattern, resp)]
        if hacked:
            print(f"[WARNING] HACKED RESPONSES: {hacked}")
        responses_str = [re.sub(hack_pattern, '', resp) for resp in responses_str]
        
        responses = self._batch_tokenize(responses_str)
        return responses, responses_str


    def _process_next_obs(self, next_obs: List[str]) -> torch.Tensor:
        """Process next observations from environment."""
        next_obs_ids = self.tokenizer(
            next_obs, 
            padding='longest',
            return_tensors='pt'
        )['input_ids']
        
        if next_obs_ids.shape[1] > self.config.max_obs_length:
            print("[WARNING] OBSERVATION TOO LONG, CONSIDER CHANGING YOUR CONFIG")
            next_obs_ids = next_obs_ids[:, :self.config.max_obs_length]
            
        return next_obs_ids

    def _update_rolling_state(self, rollings, cur_responses: torch.Tensor, 
                            next_obs_ids: torch.Tensor) -> Dict:
        """Update rolling state with new responses and observations."""
        # Concatenate and handle padding
        new_input_ids = self.tensor_fn.concatenate_with_padding([
            rollings.batch['input_ids'],
            cur_responses,
            next_obs_ids
        ])
        
        # Create attention mask and position ids
        new_attention_mask = self.tensor_fn.create_attention_mask(new_input_ids)
        new_position_ids = self.tensor_fn.create_position_ids(new_attention_mask)

        # Cut to appropriate length
        effective_len = new_attention_mask.sum(dim=1).max()
        max_len = min(self.config.max_prompt_length, effective_len)
        
        return DataProto.from_dict({
            'input_ids': new_input_ids[:, :max_len],
            'position_ids': new_position_ids[:, :max_len],
            'attention_mask': new_attention_mask[:, :max_len]
        })

    def _update_right_side(self, right_side: Dict, 
                          cur_responses: torch.Tensor,
                          next_obs_ids: torch.Tensor) -> Dict:
        """Update right side state."""
        responses = self.tensor_fn.concatenate_with_padding([
            right_side['responses'],
            cur_responses,
            next_obs_ids
        ], pad_to_left=False)
        
        effective_len = self.tensor_fn.create_attention_mask(responses).sum(dim=1).max()
        max_len = min(self.config.max_prompt_length, effective_len)
        
        return {'responses': responses[:, :max_len]}


    def run_llm_loop(self, gen_batch, envs: List[Any],
                    initial_input_ids: torch.Tensor,
                    output_dir: str,
                    global_steps: int) -> Tuple[Dict, Dict]:
        """Run main LLM generation loop."""
        # Setup visualization and Initialize states
        trajectory = self._setup_visualization()
        
        original_left_side = {'input_ids': initial_input_ids[:, -self.config.max_start_length:]}
        original_right_side = {'responses': initial_input_ids[:, []]}
        
        active_mask = torch.ones(gen_batch.batch['input_ids'].shape[0], dtype=torch.bool)
        active_num_list = [active_mask] 
        rollings = gen_batch


        # Main generation loop
        for step in range(self.config.max_turns):
            if not active_mask.sum():
                break
            rollings.batch = self.tensor_fn.cut_to_effective_len(
                rollings.batch,
                keys=['input_ids', 'attention_mask', 'position_ids']
            )
            
            # gen_output = self.actor_rollout_wg.generate_sequences(rollings)
            rollings_active = DataProto.from_dict({
                k: v[active_mask] for k, v in rollings.batch.items()
            })
            gen_output = self.actor_rollout_wg.generate_sequences(rollings_active)

            meta_info = gen_output.meta_info            
            responses_ids, responses_str = self._postprocess_responses(gen_output.batch['responses'])
            responses_ids, responses_str = self.tensor_fn._example_level_pad(responses_ids, responses_str, active_mask)

            # Update visualization
            self._update_trajectory(trajectory, envs, responses_str, active_mask)

            # Execute in environment and process observations
            next_obs, dones = self.env_class.execute_predictions(
                envs, responses_str, self.tokenizer.pad_token
            )
            active_mask = torch.tensor([not done for done in dones], dtype=torch.bool)
            active_num_list.append(active_mask.sum().item())
            next_obs_ids = self._process_next_obs(next_obs)
            
            # Update states
            rollings = self._update_rolling_state(
                rollings,
                responses_ids,
                next_obs_ids
            )
            original_right_side = self._update_right_side(
                original_right_side,
                responses_ids,
                next_obs_ids
            )
        print("ACTIVE_TRAJ_NUM:", active_num_list)

        # Save trajectory and return final output
        self._save_trajectory(trajectory, output_dir, global_steps)
        return self._compose_final_output(original_left_side, original_right_side, meta_info)

    def _setup_visualization(self) -> List[Dict]:
        """Setup visualization tracking if enabled."""
        if not self.config.logging.log_images:
            return None
        return [defaultdict(list) for _ in range(self.config.logging.log_n_image_per_batch)]

    def _update_trajectory(self, trajectory: List[Dict], 
                         envs: List[Any], responses: List[str], active_mask: torch.Tensor):
        """Update visualization trajectory if enabled."""
        if not trajectory:
            return
        n_visualize = self.config.logging.log_n_image_per_batch
        for idx, (env, active) in enumerate(zip(envs[:n_visualize], active_mask[:n_visualize])):
            if active:
                trajectory[idx]['img_before_action'].append(env.render('rgb_array'))
            
        for idx, (response, env, active) in enumerate(zip(responses[:n_visualize], 
                                                envs[:n_visualize],
                                                active_mask[:n_visualize])):
            if active:
                img_after = env.render('rgb_array')
                parsed = parse_llm_output(response, strategy="raw")
                
                trajectory[idx]['img_after_action'].append(img_after)
                trajectory[idx]['answer'].append(response)
                trajectory[idx]['parsed_response'].append(parsed)

    def _save_trajectory(self, trajectory: List[Dict], 
                        output_dir: str, global_steps: int):
        """Save trajectory visualization if enabled."""
        if not trajectory:
            return
            
        save_step_size = self.config.logging.log_image_step_size
        if not global_steps % save_step_size:
            os.makedirs(output_dir, exist_ok=True)
            save_trajectory_to_output(trajectory, save_dir=output_dir)

    def _compose_final_output(self, left_side: Dict,
                            right_side: Dict,
                            meta_info: Dict) -> Tuple[Dict, Dict]:
        """Compose final generation output."""
        final_output = right_side.copy()
        final_output['prompts'] = left_side['input_ids']
        
        # Combine input IDs
        final_output['input_ids'] = torch.cat([
            left_side['input_ids'],
            right_side['responses']
        ], dim=1)
        
        # Create attention mask and position ids
        final_output['attention_mask'] = torch.cat([
            self.tensor_fn.create_attention_mask(left_side['input_ids']),
            self.tensor_fn.create_attention_mask(final_output['responses'])
        ], dim=1)
        
        final_output['position_ids'] = self.tensor_fn.create_position_ids(
            final_output['attention_mask']
        )
        
        final_output = DataProto.from_dict(final_output)
        final_output.meta_info.update(meta_info)
        
        return final_output